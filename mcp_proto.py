"""
AI の接続口（MCP、試作・段1）。

利用者の Claude（や ChatGPT）のコネクタに「その人専用の秘密の URL」を1回登録すると、
AI がこのサービスの道具（status / next_task / submit / read）を使えるようになる。

  https://<公開ドメイン>/mcp/<秘密の文字列>

- 載せ方: main.py から attach(app) を呼ぶ。一番外側の ASGI ミドルウェアで /mcp/<秘密> を受け、
  MCP SDK（mcp 2.x）のセッション管理に直接渡す。既存の Basic 認証は通らない
- 秘密の文字列 → 利用者 の対応は、リポジトリ外・output 外のファイル（既定 ~/ugc-secrets/mcp_users.json）。
  秘密そのものでなく SHA-256 を置く。知らない文字列には 404
- 道具が触るのは分析フォルダ（output/analyses/<分析ID>/）の読み書きだけ。TikTok や Windows には届かない
- 読み込みや起動に失敗しても、既存の Web サービスは今までどおり動く（main.py 側で try/except）

利用者の追加（運営用）:
  python mcp_proto.py add-user <利用者ID> <表示名>   … 秘密の文字列を1回だけ表示し、対応表にはハッシュを書く
"""
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import time
from pathlib import Path

import proto_runner as runner

logger = logging.getLogger("mcp")

PREFIX = "/mcp/"
USERS_FILE = Path(os.environ.get("UGC_MCP_USERS", Path.home() / "ugc-secrets" / "mcp_users.json"))

# いつ使うか（2026-10-05 ユーザー「UGC Analyzer を使いたくない時はどうしたらいいの？」）:
# 新しい分析は名指しのときだけ。すでにある分析の続き・やめる・直しは名指しが無くても（status で確かめてから）
USE_RULE = (
    "使うのは次のときだけ。"
    "(1) 利用者が「UGC Analyzer で〇〇／△△を分析して」と名指しして新しい分析を頼んだとき（start_analysis）。"
    "名指しの無い「〇〇を分析して」には使わず、道具なしでふつうに答える。"
    "(2) すでにある分析のことを頼まれたとき（「〇〇の分析を続けて」「〇〇の取得をやめて」「〇〇はどうなってる？」"
    "「〇〇のレポートの3章に〜を足して」など）。分析があるか分からなければ、まず status（読むだけ）で確かめ、"
    "その曲の分析が無ければ、道具なしでふつうに答える。"
    "(3) 分析の会話の途中の返事（界隈の案への「OK」など）。"
    "今後ずっと使う設定・指示書・知識ベースの頼みは、「UGC Analyzer の〜」と名指しがあるか、分析の会話の途中のときだけ。"
)

# 開始の返事が途切れたとき（2026-10-06 友達の試し: Claude のチャットは道具1回の待ちが約60秒までで、楽曲ページを探す start_analysis が
# それを超えた。Mac は裏で探し終えて取得を受け付けていたが、AI は「返事が途中で途切れたので、どのページか確認できていない」と答えた）
START_CUT_RULE = (
    "start_analysis の返事が時間切れ・エラーで途切れたら、start_analysis を呼び直さずに status を見る"
    "（Mac は裏で楽曲ページを探し終えて、取得を受け付けていることが多い）。受け付けていれば、status にある楽曲ページ（題・作者・UGC 数・URL）を、"
    "start_analysis の返事のときと同じように利用者に伝える（「途切れた」「確認できていない」とは言わない）。"
    "その曲の分析がまだ無ければ、start_analysis を呼び直す。"
)

INSTRUCTIONS = (
    "UGC Analyzer（TikTok の楽曲 UGC 分析サービス）への接続です。分析の手順・指示書・検査はサービスが持っていて、"
    "あなたは「次の仕事を聞く → 指示書どおりにやる → 返す」を繰り返す係です。\n"
    + USE_RULE + "\n"
    + runner.REPEAT_RULE
    + "\n分析 ID が分からなければ、曲名をそのまま渡すか、status で一覧を見る。"
    + "\nレポートの完成後に利用者が直しを頼んだら revise（このレポートだけ）。ある界隈の話が浅い・もっとコメントを取って掘り下げて、"
    "と頼まれたら deepen。界隈の分け方そのものを変えたい（分ける・まとめる・切り直す）と頼まれたら recut。"
    "今後ずっと続く指示の変更は settings（利用者が頼んだときだけ使う）。"
    "\n利用者が「界隈の確認はいらない」「確認なしで最後まで書いて」と頼んだら、そのとき呼ぶ start_analysis か next_task に skip_confirm=true を付ける"
    "（界隈の案で止まらず、案のまま最後まで書く。使った界隈は完了の知らせで伝わる）。"
    + "\n" + START_CUT_RULE
)


# ---------------------------------------------------------------------------
# 秘密の文字列 → 利用者
# ---------------------------------------------------------------------------
class _Users:
    def __init__(self, path: Path):
        self.path = path
        self._mtime = None
        self._by_hash = {}

    def _reload(self):
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._by_hash, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self._by_hash = {u["token_sha256"]: u for u in data.get("users", []) if u.get("token_sha256")}
        self._mtime = mtime

    def lookup(self, token: str):
        if not token or len(token) < 32:
            return None
        self._reload()
        h = hashlib.sha256(token.encode("utf-8")).hexdigest()
        user = self._by_hash.get(h)
        if user and user.get("disabled"):
            return None
        return user


_users = _Users(USERS_FILE)


def add_user(user_id: str, name: str) -> str:
    """利用者を足して、秘密の文字列を返す（対応表にはハッシュだけを書く）"""
    token = secrets.token_urlsafe(32)  # 43文字
    data = {"users": []}
    if USERS_FILE.exists():
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    data["users"].append({"user_id": user_id, "name": name,
                          "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                          "token_hint": token[:4]})
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return token


# ---------------------------------------------------------------------------
# MCP の道具
# ---------------------------------------------------------------------------
def _build_server(user_of=None, local: bool = False):
    """道具をそろえた MCP サーバー。

    - Windows 機のサービス（attach）: 利用者は秘密の URL から分かる
    - 取得アプリ（各自の Mac で全部を回す形。collector/collector_app/mcp_local.py）: user_of で利用者を固定し、
      local=True で「指示書の編集」と「知識ベースの更新」の道具を足す
    """
    from mcp.server.mcpserver import Context, Image, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    mcp = MCPServer(name="ugc-analyzer", title="UGC Analyzer" if local else "UGC Analyzer（試作）",
                    instructions=INSTRUCTIONS, version="app-1" if local else "proto-1")

    def _user(ctx: Context) -> str:
        if user_of is not None:
            return user_of(ctx)
        req = getattr(ctx.request_context, "request", None)
        user = (getattr(req, "scope", None) or {}).get("ugc_user")
        if not user:
            raise ToolError("接続の利用者が分かりません。接続の URL を確かめてください")
        return user["user_id"]

    _seen_clients: set = set()

    def _client_name(ctx: Context) -> str:
        """道具を呼んだ AI のアプリの名乗り（初期化のときの clientInfo.name）。分からなければ空"""
        try:
            name = str(ctx.session.client_params.client_info.name or "").lower()
        except Exception:
            name = ""
        if name not in _seen_clients:   # どの AI のアプリが何と名乗るかを記録に残す（Code タブの見分けの確かめ）
            _seen_clients.add(name)
            logger.info("道具を呼んだ AI のアプリ: %s", name or "（名乗りなし）")
        return name

    def _code_tab(ctx: Context) -> bool:
        """Claude の Code タブ（Claude Code）から呼ばれたか。Code tab は裏のコマンドの終わりで起こし直されるので、待ってから続けられる"""
        n = _client_name(ctx)
        return "claude-code" in n or "claude code" in n

    def _waiting_text(ctx: Context, r: dict) -> str:
        """待ちの返事。Code タブから呼ばれたときだけ、作業フォルダの ./ugc-wait を裏で走らせて続ける一文を足す（docs/CODE_TAB_ONE_SITTING.md）。
        待てば進む待ち（取得中・取り足し中・順番待ち。state が無い start_analysis・deepen・restart_analysis の返事も）だけに付ける。
        利用者が何かするまで進まない待ち（アプリが動いていない・ログイン待ち・止まった）には「済んだら、ここで続けて」の言い方を、
        やめた分析には何も足さない（返事にやり直し方が書いてある。2026-10-06 通し試験）。
        取得が済んだ曲の頼み直し（start_analysis の「このまま書き始めます」。kind が resume）は待ちではないので、何も足さない"""
        text = r["text"]
        if r.get("kind") == "resume":
            return text
        code_tab = _code_tab(ctx)
        if code_tab and r.get("analysis_id") and (r.get("kind") == "wait" or "その間 AI は待てない" in text):
            state = r.get("state")
            if state in (None, "acquiring", "deepening", "behind"):
                text += (f"\n（AI へ: いまは Claude の Code タブから呼ばれている。上の「〇〇の分析を続けて」と頼む一文は利用者に伝えず、"
                         "「集め終わったら、このまま自動で続けます。Mac と Claude は開いたままにしてください」と伝える。"
                         f"そのあと Bash で `./ugc-wait {r['analysis_id']}` を裏で（run_in_background）走らせて止まる。"
                         "終わった知らせが来たら next_task から続ける（作業フォルダの CLAUDE.md のとおり）。）")
            elif state != "cancelled":
                text += ("\n（AI へ: いまは Claude の Code タブから呼ばれている。この待ちは、利用者が上のことを済ませるまで進まないので、"
                         "./ugc-wait は走らせない。上の内容を利用者に伝え、「済んだら、この会話で『続けて』と言ってください」と添えて止まる。"
                         "「続けて」と言われたら next_task から続ける。）")
        return text

    def _call(fn, *args):
        t0 = time.time()
        try:
            return fn(*args)
        except runner.RunnerError as e:
            raise ToolError(str(e)) from e
        except Exception as e:
            logger.exception("道具の実行で例外: %s", fn.__name__)
            raise ToolError(f"サービス側のエラーです（{type(e).__name__}）。少し待って同じ道具を呼び直してください") from e
        finally:   # 道具ごとにかかった時間（Claude のチャットは道具1回 約60秒まで。超えたかを記録で見られるように）
            logger.info("道具の中身 %s: %.1f秒", fn.__name__, time.time() - t0)

    rule = "\n\n繰り返し方: " + runner.REPEAT_RULE

    @mcp.tool(
        title="分析を始める",
        description=("TikTok の楽曲の UGC 分析を始める（取得を待ち行列に入れる）。"
                     "**利用者が「UGC Analyzer で〇〇／△△を分析して」と名指しして頼んだときだけ使う**"
                     "（名指しの無い「〇〇を分析して」には使わない。restart_analysis の返事で呼び直すように言われたときは呼ぶ）。"
                     "song は曲名、artist はアーティスト名。music_url は TikTok の楽曲ページ（https://www.tiktok.com/music/…）。"
                     + ("楽曲ページは Mac が TikTok で探す（曲名で人気の動画を開いて使われている音源を読み、楽曲ページの UGC 数を比べ、"
                        "一番使われているページと、その3割以上使われている同じ曲の公式のページ（sped up 版など）を合わせて取る。数十秒かかる）。"
                        "**まず曲名とアーティスト名だけで呼んでよい**。ウェブ検索で楽曲ページ（https://www.tiktok.com/music/…）や、"
                        "その曲を使った動画（https://www.tiktok.com/@…/video/…）がすでに見つかっていれば、candidate_urls・video_urls に足してもよい。"
                        "Mac が見つけられなかったときや根拠が足りないときは、探し方が返ってくるので従って呼び直す。"
                        "利用者が楽曲ページの URL を渡したときは、探し直さずに全部を music_urls に入れる（2つ以上なら全部から取って合わせる）。"
                        "利用者に URL を頼むのは、何通りも検索して見つからないときの最後の手段。利用者に確認は求めない。"
                        "取得を始めると、どの楽曲ページで進めるか（題・作者・UGC 数・URL）が返ってくるので、それを利用者に伝える。"
                        "取得は利用者の Mac の UGC Analyzer が2〜3時間ほどかけてやり、終わると Mac の通知が出る。" if local else
                        "分かれば music_url。取得は半日ほどかかる。")
                     + "replies は、利用者が返信（コメントへの返信）も取るように頼んだときだけ true。既定は取らない"
                     "（取るとコメントの取得が2〜3割長くなる）。"
                     "min_plays は、週ごとに選ぶ動画の再生の下限（既定10万。起点・大型ヒット・本人・公式などは再生に関わらず取る）。"
                     "利用者が「再生〇万以上の動画だけで」「小さい動画も見て」などと頼んだときだけ数で指定する。"
                     "skip_confirm は、利用者が「界隈の確認はいらない」「確認なしで最後まで書いて」と頼んだときだけ true（取得のあと、界隈の案で止まらずに最後まで書く）。"
                     + "返ってきた内容を利用者に短く伝えて止まる（取得を待たない・見に来ない）。"
                     "ただし返事に「このまま書き始めます」とあれば（その曲はもう集め終わっている）、止まらずに、"
                     "「〇〇の分析を続けて」と言われたときと同じに next_task から進める。" + START_CUT_RULE + rule),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                    open_world_hint=True),
    )
    async def start_analysis(ctx: Context, song: str, artist: str = "", music_url: str | None = None,
                             video_url: str | None = None, candidate_urls: list[str] | None = None,
                             only_one: bool = False, music_urls: list[str] | None = None,
                             video_urls: list[str] | None = None, replies: bool = False,
                             min_plays: int | None = None, skip_confirm: bool = False) -> str:
        return _waiting_text(ctx, _call(runner.start_analysis, _user(ctx), song, artist, music_url or "", video_url or "",
                                        candidate_urls or [], only_one, music_urls or [], video_urls or [], bool(replies),
                                        min_plays, bool(skip_confirm)))

    @mcp.tool(
        title="分析の状態",
        description="利用者の分析の一覧と状態（取得中・AI の番・確認待ち・完了）を返す。"
                    "利用者に進み具合を聞かれたとき、または分析 ID が分からないときに使う。" + rule,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def status(ctx: Context, analysis_id: str | None = None) -> str:
        r = _call(runner.status, _user(ctx), analysis_id)
        return r["text"] + "\n\n```json\n" + json.dumps(r["analyses"], ensure_ascii=False) + "\n```"

    @mcp.tool(
        title="次の仕事",
        description="分析の次の仕事を1つ受け取る（task_id・kind・指示書・入力・出力の形）。"
                    "analysis_id は分析 ID か曲名（省略するとその利用者の分析）。"
                    "kind: ai=指示書どおりにやって submit／ask_user=内容を利用者に見せて答えを submit／"
                    "wait=取得中なので止まる／done=完了なので止まる。"
                    "skip_confirm=true は、利用者が「界隈の確認はいらない」「確認なしで最後まで書いて」と頼んだとき（界隈の確認で止まらず、案のまま進める）。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                    open_world_hint=False),
    )
    async def next_task(ctx: Context, analysis_id: str | None = None, skip_confirm: bool = False) -> str:
        return _waiting_text(ctx, _call(runner.next_task, _user(ctx), analysis_id, bool(skip_confirm)))

    @mcp.tool(
        title="結果を返す",
        description="仕事の結果を返す。output は指示書の「出力の形」のテキスト（TSV か JSON）。"
                    "サービスが検査し、良ければ ok=true（続けて next_task を呼ぶ）、だめなら ok=false と理由の一覧"
                    "（直して同じ task_id で出し直す）。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                    open_world_hint=False),
    )
    async def submit(ctx: Context, task_id: str, output: str) -> str:
        r = _call(runner.submit, _user(ctx), task_id, output)
        head = {"ok": r["ok"], "progress": r.get("progress")}
        return json.dumps(head, ensure_ascii=False) + "\n" + r["text"]

    @mcp.tool(
        title="レポートを直す",
        description="完成したレポートを、利用者の指示どおりに直す（このレポートだけ）。利用者がレポートを読んで"
                    "「3章に音楽面の話を足して」のように頼んだときに使う。instruction は利用者の言葉そのまま。"
                    "受け付けたら next_task で直しの仕事を片付ける。何度でも使える。"
                    "界隈の分け方そのものを変えたいときは recut。"
                    "「今後ずっと」の指示（文体など）は revise ではなく settings。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                    open_world_hint=False),
    )
    async def revise(ctx: Context, instruction: str, analysis_id: str | None = None) -> str:
        return _call(runner.revise, _user(ctx), analysis_id, instruction)["text"]

    @mcp.tool(
        title="指示の設定",
        description="**利用者が設定の確認や変更を頼んだときだけ使う**（自分の判断では使わない）。"
                    "次の分析以降もずっと使う指示を、利用者ごとに見る・変える・デフォルトに戻す。"
                    "action: get（見る）/ set（変える）/ reset（デフォルトに戻す、item を省くと全部）。"
                    "item: style（文体。value は追記したい指定、use_style_guide=false で著者の文体ガイドを使わない）／"
                    "focus（レポートの重点）／community_policy（界隈の分け方の方針）／comment_lens（コメント分析の観点）。"
                    "各400字まで。出力の形・検算・仕事の刻み方は変えられない。"
                    "「このレポートだけ」の直しは settings ではなく revise。",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                    open_world_hint=False),
    )
    async def settings(ctx: Context, action: str = "get", item: str | None = None, value: str | None = None,
                       use_style_guide: bool | None = None) -> str:
        return _call(runner.settings, _user(ctx), action, item, value, use_style_guide)["text"]

    @mcp.tool(
        title="資料を読む",
        description="仕事に要る資料を読む。name は指示書の「読む資料」にある名前"
                    "（例: taxonomy_sample、sheet:03、xsheet:00、comments:22、kb:glossary、kb:style、kb:cards、"
                    "note:<過去レポート>、past:1、chapter:branch、synthesis、pathway、taxonomy）。"
                    "長い資料は page で分かれている（1から）。sheet:NN・xsheet:NN はサムネイルの一覧画像を返す。" + rule,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def read(ctx: Context, name: str, analysis_id: str | None = None, page: int = 1):
        r = _call(runner.read, _user(ctx), analysis_id, name, page)
        if "image" in r:
            return [r["text"], Image(data=r["image"], format=r["format"])]
        return r["text"]

    if not local:
        return mcp

    @mcp.tool(
        title="界隈を掘り下げる",
        description="完成したレポートの、ある界隈の話を掘り下げる（このレポートだけ）。利用者がレポートを読んで"
                    "「△△界隈のところが浅い」「なぜバズったのかが弱い」「もっとコメントを取って掘り下げて」のように頼んだときに使う。"
                    "まず Mac がその界隈のコメントを取り足す（25分ほどまで。終わると Mac の通知が出る）。そのあと利用者が「〇〇の分析を続けて」と言ったら、"
                    "next_task で、その界隈の分析のやり直し・構成案と関わる章の書き直し・全章の通し読みを片付ける。"
                    "community は界隈の key か、レポートでの呼び名"
                    "（当たらなければ界隈の一覧が返るので、key を選んで呼び直す。利用者には聞かない）。"
                    "instruction は利用者の言葉そのまま。章の言い回しや足したい考察だけの直しは revise、界隈の分け方を変えたいときは recut。"
                    "返ってきた内容を利用者に短く伝えて止まる（取り足しを待たない）。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                    open_world_hint=True),
    )
    async def deepen(ctx: Context, community: str, instruction: str, analysis_id: str | None = None) -> str:
        return _waiting_text(ctx, _call(runner.deepen, _user(ctx), analysis_id, community, instruction))

    @mcp.tool(
        title="界隈を切り直す",
        description="完成したレポートの界隈の分け方を、利用者の指示どおりに変えて、レポートを新しい版に書き直す（このレポートだけ）。"
                    "利用者がレポートを読んで「〇〇界隈を2つに分けて」「△△と□□をまとめて」「動機で切り直して」「界隈の切り直しからやり直して」"
                    "のように、界隈の分け方そのものを変えたいと頼んだときに使う。instruction は利用者の言葉そのまま。"
                    "受け付けたら next_task で、界隈の切り直し → 動画のラベルの付け直しを片付ける（利用者に確認は取らない）。"
                    "新しい界隈で読むべき動画にコメントが無ければ Mac が取り足すので、kind が wait になったところで止まり、"
                    "返ってきた一文を利用者に伝える（終わったあと利用者が「〇〇の分析を続けて」と言ったら next_task で続ける）。"
                    "足りていれば止まらずに、新しい版の完成（kind が done）まで next_task を続ける。"
                    "ある界隈の話が浅いだけなら deepen、章の言い回しや考察の足しは revise。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                    open_world_hint=True),
    )
    async def recut(ctx: Context, instruction: str, analysis_id: str | None = None) -> str:
        return _call(runner.recut, _user(ctx), analysis_id, instruction)["text"]

    @mcp.tool(
        title="指示書を見る・直す",
        description="**利用者が、AI に渡す指示書そのものを見たい・変えたいと頼んだときだけ使う**（自分の判断では使わない）。"
                    "action: list（一覧）/ get（name の全文を見る）/ set（name を text の全文で置き換える）/ reset（初期に戻す。name を省くと全部）。"
                    "変えるときは、get で今の全文を読み、利用者の頼みどおりに直した全文を set で渡す。"
                    "{{…}} の差し込みの印は消さない。「### 出力の形」の節は直しても使われない（検査が決まった形を前提にしている）。"
                    "文体・重点などの短い追記で足りる頼みは settings のほうが軽い。",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                    open_world_hint=False),
    )
    async def prompts(ctx: Context, action: str = "list", name: str | None = None, text: str | None = None) -> str:
        return _call(runner.prompts, _user(ctx), action, name, text)["text"]

    @mcp.tool(
        title="取得をやめてやり直す",
        description="**利用者が「〇〇の取得をやめて、このページでやり直して」「〇〇の取得をやめて、最初からやり直して」「外したページも入れて」のように頼んだときだけ使う**。"
                    "取得中・順番待ち・止まった分析の取得をやめ、music_url（https://www.tiktok.com/music/…）があればその楽曲ページで取り直す。"
                    "楽曲ページを2つ以上で取り直すとき（外したページを足すときは、進めていたページも含めて全部）は music_urls に入れる（全部から取って合わせる）。"
                    "music_url を省くと、楽曲ページ探しから最初にやり直す（返ってくる探し方に従って検索し、start_analysis を呼ぶ）。"
                    "replies は返信も取るか、min_plays は週ごとに選ぶ動画の再生の下限（どちらも省くと前の分析の指定を引き継ぐ。利用者が頼んだときだけ渡す）。"
                    "analysis_id は分析 ID か曲名。返ってきた内容を利用者に短く伝える。"
                    "analysis_id を省いて、やり直せる分析が2つ以上あると、どれもやめずに一覧が返るので、どれをやり直すか利用者に1回聞いて、"
                    "答えの曲名か分析 ID で呼び直す。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                    open_world_hint=True),
    )
    async def restart_analysis(ctx: Context, music_url: str | None = None, analysis_id: str | None = None,
                               music_urls: list[str] | None = None, replies: bool | None = None,
                               min_plays: int | None = None) -> str:
        return _waiting_text(ctx, _call(runner.restart_analysis, _user(ctx), analysis_id, music_url or "", music_urls or [],
                                        replies, min_plays))

    @mcp.tool(
        title="取得をやめる",
        description="**利用者が「〇〇の取得をやめて」と頼んだときだけ使う**（やり直しまで頼まれたら restart_analysis）。"
                    "取得中・順番待ちの分析の取得をやめる。analysis_id は分析 ID か曲名。"
                    "analysis_id を省いて、やめられる分析が2つ以上あると、どれもやめずに一覧が返るので、どれをやめるか利用者に1回聞いて、"
                    "答えの曲名か分析 ID で呼び直す。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True,
                                    open_world_hint=False),
    )
    async def cancel_analysis(ctx: Context, analysis_id: str | None = None) -> str:
        return _call(runner.cancel_analysis, _user(ctx), analysis_id)["text"]

    @mcp.tool(
        title="知識ベースを新しくする",
        description="著者（山本慶太朗）の note の新しい記事を確かめ、知識ベースに取り込む準備をする。"
                    "利用者が「UGC Analyzer の知識ベースを更新して」と頼んだときに使う（UGC Analyzer も週1回、自動で確かめている）。"
                    "取り込む記事があれば、そのあと next_task を呼ぶと取り込みの仕事が渡される。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                    open_world_hint=True),
    )
    async def update_knowledge(ctx: Context) -> str:
        return _call(runner.update_knowledge, _user(ctx))["text"]

    return mcp


# ---------------------------------------------------------------------------
# FastAPI に載せる
# ---------------------------------------------------------------------------
class _McpGate:
    """/mcp/<秘密> だけを受ける一番外側の ASGI ミドルウェア。それ以外は素通し"""

    def __init__(self, app, session_manager=None, state=None):
        self.app = app
        self.session_manager = session_manager
        self.state = state

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(PREFIX):
            await self.app(scope, receive, send)
            return
        token = scope["path"][len(PREFIX):].rstrip("/")
        user = _users.lookup(token) if "/" not in token else None
        if user is None:
            client = (scope.get("client") or ("?",))[0]
            for k, v in scope.get("headers", []):
                if k == b"x-forwarded-for":
                    client = v.decode("latin-1").split(",")[0].strip()
            logger.warning("接続口: 知らない URL への要求を 404 で返しました ip=%s hint=%s", client, token[:4])
            await _plain(send, 404, b"404 Not Found")
            return
        if not self.state.get("ready"):
            await _plain(send, 503, b"503 MCP not ready")
            return
        if scope.get("method") == "GET":
            # サーバーからの通知の流れ（GET の SSE）は使わない。SDK は stateless でも GET を開いたまま持つので、仕様どおり 405 で断る
            await _plain(send, 405, b"405 Method Not Allowed", [(b"allow", b"POST, DELETE")])
            return
        scope = dict(scope)
        scope["ugc_user"] = {"user_id": user["user_id"], "name": user.get("name", "")}
        await self.session_manager.handle_request(scope, receive, send)


async def _plain(send, status: int, body: bytes, extra_headers: list | None = None):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())] + (extra_headers or [])})
    await send({"type": "http.response.body", "body": body})


class _MaskSecretFilter(logging.Filter):
    """アクセスログの /mcp/<秘密> を伏せる"""

    _re = re.compile(r"/mcp/[^/\s\"?]+")

    def filter(self, record):
        try:
            if record.args and isinstance(record.args, tuple):
                record.args = tuple(self._re.sub("/mcp/***", a) if isinstance(a, str) else a for a in record.args)
            elif isinstance(record.msg, str):
                record.msg = self._re.sub("/mcp/***", record.msg)
        except Exception:
            pass
        return True


def attach(app) -> None:
    """既存の FastAPI アプリに接続口を載せる。失敗したら例外（main.py 側で握って警告だけ出す）"""
    from mcp.server.transport_security import TransportSecuritySettings

    mcp = _build_server()
    # 公開 URL＋秘密のパスで守る。Host/Origin の検査（localhost 向けの DNS rebinding 対策）は切る
    mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    session_manager = mcp.session_manager
    state = {"ready": False}

    original = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        async with contextlib.AsyncExitStack() as stack:
            try:
                await stack.enter_async_context(session_manager.run())
                state["ready"] = True
                logger.info("AI の接続口（MCP）を開きました: %s<秘密> 利用者表=%s", PREFIX, USERS_FILE)
            except Exception:
                logger.exception("AI の接続口（MCP）を開けませんでした。Web サービスはそのまま動きます")
            async with original(a) as maybe_state:
                yield maybe_state

    app.router.lifespan_context = lifespan
    app.add_middleware(_McpGate, session_manager=session_manager, state=state)
    logging.getLogger("uvicorn.access").addFilter(_MaskSecretFilter())


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "add-user":
        tok = add_user(sys.argv[2], sys.argv[3])
        print("秘密の文字列（この1回しか表示しません。URL は /mcp/<これ>）:")
        print(tok)
    else:
        print("使い方: python mcp_proto.py add-user <利用者ID> <表示名>")

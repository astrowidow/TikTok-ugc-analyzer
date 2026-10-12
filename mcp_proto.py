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

# 分析・アプリについての質問と、やり直しの頼み（2026-10-12 友達の会話: Claude が道具で答えられず、Bash でアプリのフォルダを覗いて
# 「60秒の音源に入っている可能性が高い」と推測で答えた。想定どおりのログの英語の一行を読み違えて、できない通知の設定を案内した。
# 取り直しを曲名を変えて何度も呼び、できない取り直しを「します」と言った。docs/PLAN_0_8.md 第3章）。
# ChatGPT のスキル（collector_app/codex_link.SKILL_MD）と Code タブの CLAUDE.md（code_link.CLAUDE_MD）にも同じ文を入れる
ASK_RULE = (
    "分析やアプリについての質問（どの楽曲ページ・音源を取ったか、ファンの音源を探したか、この動画・音源は入っているか、何本・何件取ったか、"
    "取得の版・日時・設定、レポートの版、ログイン・通知・アプリの版）には、まず info（読むだけ）で確かめてから答える。"
    "道具の返事に無いことは「UGC Analyzer の記録には無い」と言い、推測で埋めない。推測を言うときは推測だと言い、確かめ方を添える。"
    "Bash などでアプリのフォルダ（~/Library/Application Support/UGC Analyzer）やログを直接開かない・書き換えない・動かさない"
    "（ログの英語のエラー文には想定どおりのものがある）。利用者に案内する設定の操作は、道具の返事にあるものだけ。"
    "\n「最初から」「やり直して」「取り直して」「このページも足して・入れて」「〇〇からやり直して」の頼みには、道具で応える: "
    "TikTok から集め直す頼み（最初から・ページを足す／外す・ファンの音源・もっと多くの動画・新しい投稿・再生の下限・返信）は restart_analysis、"
    "集めたデータのまま AI の作業を段からやり直す頼み（界隈の案から・段階から・構成から・〇章を書き直して など）は redo。"
    "どちらも完成の前でも後でも使える。時間がかかるときは、道具が返す見込みを利用者に一度伝えてそのまま進める（確かめを求めない）。"
    "道具が断ったら、その理由を利用者にそのまま伝える。同じ頼みを言い換えて何度も呼ばない。できないことを「します」と言わない。"
    "\nほかの頼みと道具（どれも完成の前でも後でも使える。書いている途中なら道具が積んで、終わったら始める）: "
    "章を足す・外す・並べ替える（「音楽の章はいらない」「〇〇の章を足して」）は restructure、"
    "動画を外す・戻す・足す（「この動画は関係ないから外して」「この動画も入れて」）は videos（exclude・include・add）、"
    "動画ごと・全体のコメントの取り足し（「この動画のコメントを全部読んで」「全体でコメントをもっと」）は deepen（videos・community=\"all\"・per_video）、"
    "比べる（「前回からどれだけ伸びた？」「〇〇と△△を比べて」）は compare、"
    "分析を消して容量を空ける（「古い分析を消して」）は archive_analysis の delete（ゴミ箱へ。どれが大きいかは info の about=storage）、"
    "一人称や文体を今後ずっと変える（「一人称は私で」）は settings の style。分析を消すのは、利用者が頼んだときだけ。"
)

INSTRUCTIONS = (
    "UGC Analyzer（TikTok の楽曲 UGC 分析サービス）への接続です。分析の手順・指示書・検査はサービスが持っていて、"
    "あなたは「次の仕事を聞く → 指示書どおりにやる → 返す」を繰り返す係です。\n"
    + USE_RULE + "\n"
    + runner.REPEAT_RULE
    + "\n分析 ID が分からなければ、曲名をそのまま渡すか、status で一覧を見る。"
    + "\n利用者がレポートの直しを頼んだら revise（このレポートだけ。書いている途中でも受け付けて、今の作業のあとに積む）。ある界隈の話が浅い・もっとコメントを取って掘り下げて、"
    "と頼まれたら deepen。界隈の分け方そのものを変えたい（分ける・まとめる・切り直す）と頼まれたら recut。"
    "今後ずっと続く指示の変更は settings（利用者が頼んだときだけ使う）。"
    "\n利用者が「界隈の確認はいらない」「確認なしで最後まで書いて」と頼んだら、そのとき呼ぶ start_analysis か next_task に skip_confirm=true を付ける"
    "（界隈の案で止まらず、案のまま最後まで書く。使った界隈は完了の知らせで伝わる）。"
    + "\n" + START_CUT_RULE
    + "\n" + ASK_RULE
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
def _build_server(user_of=None, local: bool = False, version: str | None = None):
    """道具をそろえた MCP サーバー。

    - Windows 機のサービス（attach）: 利用者は秘密の URL から分かる
    - 取得アプリ（各自の Mac で全部を回す形。collector/collector_app/mcp_local.py）: user_of で利用者を固定し、
      local=True で「指示書の編集」と「知識ベースの更新」の道具を足す
    """
    from mcp.server.mcpserver import Context, Image, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    mcp = MCPServer(name="ugc-analyzer", title="UGC Analyzer" if local else "UGC Analyzer（試作）",
                    instructions=INSTRUCTIONS, version=version or ("app-1" if local else "proto-1"))

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
        # アプリを新しくしたのに、この道具の処理（Claude・ChatGPT が起こしたもの）が前の版のまま: 仕事をせずに開き直しを頼む
        # （2026-10-12: 前の版の道具が新しい版の分析を書くと材料が欠け、.app を置き換えたあとは部品を読めずに落ち続ける。docs/PLAN_0_8.md 3-4）
        check = getattr(runner.LOCAL, "version_check", None)
        if check is not None:
            try:
                stale = check()
            except Exception:
                stale = None
            if stale:
                raise ToolError(stale)
        try:
            return fn(*args)
        except runner.RunnerError as e:
            raise ToolError(str(e)) from e
        except SystemExit as e:
            # アプリを置き換えたあと、前の版の道具が部品を読めないと SystemExit になる（2026-10-12 の調べ。Exception では捕まらず処理が落ちる）
            logger.exception("道具の実行で SystemExit: %s", fn.__name__)
            raise ToolError("UGC Analyzer の部品を読めませんでした。アプリを新しくした直後なら、Claude（ChatGPT）を一度終了（⌘Q）して"
                            "開き直してください。会話と分析は消えません") from e
        except Exception as e:
            logger.exception("道具の実行で例外: %s", fn.__name__)
            if type(e).__name__ in ("error", "ZipImportError", "ModuleNotFoundError") and "zlib" in type(e).__module__ + str(e):
                raise ToolError("UGC Analyzer の部品を読めませんでした。アプリを新しくした直後なら、Claude（ChatGPT）を一度終了（⌘Q）して"
                                "開き直してください。会話と分析は消えません") from e
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
                     + "fresh は、利用者が「最初から」「最初から分析し直して」「取り直して」と頼んだときだけ true"
                     "（同じ曲を集め終わっていても書き始めず、新しく集め直す。前の分析は消さずに残る）。"
                     "返ってきた内容を利用者に短く伝えて止まる（取得を待たない・見に来ない）。"
                     "ただし返事に「このまま書き始めます」とあれば（その曲はもう集め終わっている）、止まらずに、"
                     "「〇〇の分析を続けて」と言われたときと同じに next_task から進める。"
                     "すでにある分析に楽曲ページを足す・外す・設定を変えて集め直す頼みは restart_analysis。" + START_CUT_RULE + rule),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                    open_world_hint=True),
    )
    async def start_analysis(ctx: Context, song: str, artist: str = "", music_url: str | None = None,
                             video_url: str | None = None, candidate_urls: list[str] | None = None,
                             only_one: bool = False, music_urls: list[str] | None = None,
                             video_urls: list[str] | None = None, replies: bool = False,
                             min_plays: int | None = None, skip_confirm: bool = False, fresh: bool = False) -> str:
        def start_analysis_fresh(*a):   # 記録の名前（道具の中身 〇〇: 〇秒）を start_analysis のままにする
            return runner.start_analysis(*a, fresh=True) if fresh else runner.start_analysis(*a)
        start_analysis_fresh.__name__ = "start_analysis"
        return _waiting_text(ctx, _call(start_analysis_fresh, _user(ctx), song, artist,
                                        music_url or "", video_url or "", candidate_urls or [], only_one, music_urls or [],
                                        video_urls or [], bool(replies), min_plays, bool(skip_confirm)))

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
                    "skip_confirm=true は、利用者が「界隈の確認はいらない」「確認なしで最後まで書いて」と頼んだとき（界隈の確認で止まらず、案のまま進める）。"
                    "want_confirm=true は、確認を省く頼みを取り消して「やっぱり界隈の確認をしたい」と言われたとき"
                    "（確認がもう済んでいたら、redo の from=confirm で確認からやり直す）。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                    open_world_hint=False),
    )
    async def next_task(ctx: Context, analysis_id: str | None = None, skip_confirm: bool = False,
                        want_confirm: bool = False) -> str:
        if want_confirm:
            return _waiting_text(ctx, _call(runner.next_task, _user(ctx), analysis_id, bool(skip_confirm), True))
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
        description="レポートを、利用者の指示どおりに直す（このレポートだけ。1つの章でも、複数の章・全章でもよい。書いている途中に頼まれたら、今の作業のあとに積む）。利用者がレポートを読んで"
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
                    "item: style（文体。value は追記したい指定（例「一人称は『私』」）、use_style_guide=false で著者の文体ガイドを使わない）／"
                    "focus（レポートの重点）／community_policy（界隈の分け方の方針）／comment_lens（コメント分析の観点）。"
                    "各2000字まで。出力の形・検算・仕事の刻み方は変えられない。"
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
        title="コメントを取り足して掘り下げる",
        description="レポートの、ある界隈の話・ある動画の話を、コメントを取り足して掘り下げる（このレポートだけ。書いている途中に頼まれたら、今の作業のあとに積む）。利用者がレポートを読んで"
                    "「△△界隈のところが浅い」「なぜバズったのかが弱い」「もっとコメントを取って掘り下げて」のように頼んだときに使う。"
                    "動画ごと（「この動画のコメントを全部読んで」）は videos に動画の URL か番号（seq）を並べる（community は空でよい）。"
                    "全部の界隈（「全体でコメントをもっと取って」）は community=\"all\"。"
                    "per_video は動画1本あたりに取るコメントの件数（数）か \"all\"（その動画のコメントを全部）。利用者が件数や「全部」を言ったときだけ。"
                    "まず Mac がその界隈のコメントを取り足す（時間では切らずに全部取る。かかる時間は返事に出るので伝える。終わると Mac の通知が出る）。そのあと利用者が「〇〇の分析を続けて」と言ったら、"
                    "next_task で、その界隈の分析のやり直し・構成案と関わる章の書き直し・全章の通し読みを片付ける。"
                    "community は界隈の key か、レポートでの呼び名"
                    "（当たらなければ界隈の一覧が返るので、key を選んで呼び直す。利用者には聞かない）。"
                    "instruction は利用者の言葉そのまま。章の言い回しや足したい考察だけの直しは revise、界隈の分け方を変えたいときは recut。"
                    "返ってきた内容を利用者に短く伝えて止まる（取り足しを待たない）。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                    open_world_hint=True),
    )
    async def deepen(ctx: Context, community: str = "", instruction: str = "", analysis_id: str | None = None,
                     videos: list[str] | None = None, per_video: str | None = None) -> str:
        if videos or per_video:
            return _waiting_text(ctx, _call(runner.deepen, _user(ctx), analysis_id, community, instruction, videos or None,
                                            per_video or None))
        return _waiting_text(ctx, _call(runner.deepen, _user(ctx), analysis_id, community, instruction))

    @mcp.tool(
        title="界隈を切り直す",
        description="レポートの界隈の分け方を、利用者の指示どおりに変えて、レポートを新しい版に書き直す（このレポートだけ。書いている途中に頼まれたら、今の作業のあとに積む）。"
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
        title="TikTok から集め直す",
        description="すでにある分析を、TikTok から集め直す。**どの状態の分析でも使える**（取得中・止まった・集め終わった・書いている途中・完成済み）。"
                    "利用者が「最初から取り直して」「最初からやり直して」「このページ（ファンの音源）も足して・入れて」「このページは外して」"
                    "「ファンの音源も探して」「もっと多くの動画を読んで」「新しい投稿も入れて・最新の数字で」「再生の下限を〇万に」「返信も取って」"
                    "「止まった取得を続きから」と頼んだときだけ使う（自分の判断では使わない）。"
                    "集め直すときは新しい分析を作り、前の分析とレポートは消さずに残る（前に取った属性・コメントは使い回し、重なる動画は取りに行かない）。"
                    "引数（頼まれたものだけ渡す。何も渡さなければ「最初から」＝楽曲ページ探し・ファンの音源探しから全部）: "
                    "music_url・music_urls は「このページで（入れ替える）」、add_music_urls は「このページも足す」（今のページは残る）、"
                    "remove_music_urls は「このページは外す」、fan_sounds は同じ曲のファンの音源を探すか、"
                    "read_more は詳しく読む動画の本数（数か \"all\"）、refresh=true は最新の数字で取り直す、"
                    "replies は返信も取るか、min_plays は週ごとに選ぶ動画の再生の下限、resume=true は止まった取得を済んだところの続きから（同じ分析）。"
                    "analysis_id は分析 ID か曲名。返ってきた内容（集め終わる見込みを含む）を利用者に短く伝えて止まる（確かめを求めない）。"
                    "analysis_id を省いて、当てはまる分析が2つ以上あると、どれも触らずに一覧が返るので、どれをやり直すか利用者に1回聞いて、"
                    "答えの曲名か分析 ID で呼び直す。集めたデータのまま AI の作業だけをやり直す頼みは redo。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                    open_world_hint=True),
    )
    async def restart_analysis(ctx: Context, music_url: str | None = None, analysis_id: str | None = None,
                               music_urls: list[str] | None = None, replies: bool | None = None,
                               min_plays: int | None = None, add_music_urls: list[str] | None = None,
                               remove_music_urls: list[str] | None = None, fan_sounds: bool | None = None,
                               read_more: str | None = None, refresh: bool = False, resume: bool = False) -> str:
        return _waiting_text(ctx, _call(runner.restart_analysis, _user(ctx), analysis_id, music_url or "", music_urls or [],
                                        replies, min_plays, add_music_urls or [], remove_music_urls or [], fan_sounds,
                                        read_more, bool(refresh), bool(resume)))

    @mcp.tool(
        title="取得をやめる",
        description="**利用者が「〇〇の取得をやめて」「取り足しをやめて」と頼んだときだけ使う**（やり直しまで頼まれたら restart_analysis）。"
                    "取得中・順番待ちの分析の取得と、掘り下げ・切り直しのコメントの取り足しをやめる。analysis_id は分析 ID か曲名。"
                    "analysis_id を省いて、やめられる分析が2つ以上あると、どれもやめずに一覧が返るので、どれをやめるか利用者に1回聞いて、"
                    "答えの曲名か分析 ID で呼び直す。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True,
                                    open_world_hint=False),
    )
    async def cancel_analysis(ctx: Context, analysis_id: str | None = None) -> str:
        return _call(runner.cancel_analysis, _user(ctx), analysis_id)["text"]

    @mcp.tool(
        title="AI の作業を段からやり直す",
        description="集めたデータ（TikTok から取ったもの）はそのままで、AI の作業を、指定した段からやり直す。**完成の前でも後でも使える**。"
                    "利用者が「〇〇の分析を、界隈の案から・界隈の確認から・段階の分け方から・コメントの分析から・構成から やり直して」"
                    "「参考記事を選び直して」「ウェブで調べ直して」「〇章と〇章を書き直して」「全章を書き直して」「note 用の仕上げだけやり直して」"
                    "「前の版に戻して」と頼んだときに使う。"
                    "from_step: axes（界隈の案から）／confirm（界隈の確認から。案はそのまま見せ直す）／label（ラベル付けから）／phases（段階の分け方から）／"
                    "ccomments（界隈ごとのコメントの分析から）／refs（参考記事の選び直し）／research（ウェブの調べ直し）／era（時代背景）／"
                    "outline（構成から）／write（本文の書き直し。chapters に章の名前を並べる。省けば全章）／finish（note 用の仕上げ）。"
                    "章の名前は read の chapters で分かる。instruction は利用者の言葉そのまま（どう変えたいか）。"
                    "restore に「前の版」「第1版」などを入れると、残してある前の版に戻す（from_step は要らない）。"
                    "前の版は消さずに残る。返事に見込みの時間が出るので利用者に伝え、確かめを求めずに next_task で進める。"
                    "TikTok から集め直す頼み（ページを足す・最初から取り直す など）は restart_analysis。"
                    "1つの章の言い回しや足したい考察だけなら revise でもよい。界隈の分け方を変える（分ける・まとめる）は recut。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                    open_world_hint=False),
    )
    async def redo(ctx: Context, from_step: str = "", analysis_id: str | None = None, chapters: list[str] | None = None,
                   instruction: str = "", restore: str = "") -> str:
        return _call(runner.redo, _user(ctx), analysis_id, from_step, chapters, instruction, restore)["text"]

    @mcp.tool(
        title="分析とアプリのことを調べる",
        description="読むだけ。分析やアプリについての質問に、UGC Analyzer の記録から答える。**推測で答える前に必ずこれで確かめる**。"
                    "about: overview（どの楽曲ページで取ったか・外したページと理由・集めた本数・コメントの件数・設定・取得の日時と版・AI の進み。既定）／"
                    "sounds（音源ごとの長さ・本数、同じ曲のファンの音源を探したか・見つけたもの・入れたもの・外したもの。"
                    "target に楽曲ページの URL を入れると、それが入っているか）／"
                    "video（target に動画の URL か番号。一覧にあるか・詳しく読んだか・コメントの件数・界隈）／"
                    "report（レポートの版・検算・界隈ごとの本数・ウェブで調べたか・界隈の確認の答え）／"
                    "app（アプリの版・この会話の道具の版・TikTok のログイン・最近のエラー・通知の出し方。analysis_id は要らない）／"
                    "storage（分析ごとの容量・様子と Mac の空き容量。analysis_id は要らない）。"
                    "analysis_id は分析 ID か曲名（省くとその利用者の分析）。記録に無いことは「記録に無い」と返るので、そのまま伝える。" + rule,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def info(ctx: Context, about: str = "overview", analysis_id: str | None = None, target: str | None = None) -> str:
        return _call(runner.info, _user(ctx), analysis_id, about, target or "")["text"]

    @mcp.tool(
        title="分析をしまう・戻す・消す",
        description="**利用者が「〇〇の分析をしまって・片付けて・一覧から消して」「しまった〇〇を戻して」「〇〇の分析を消して・容量を空けて」と頼んだときだけ使う**。"
                    "しまった分析は一覧・曲名での引き当てに出なくなる（消さない。レポートのフォルダもそのまま）。restore=true で戻す。"
                    "delete=true は分析のフォルダをゴミ箱に入れる（Finder のゴミ箱から「戻す」で戻せる。ゴミ箱を空にすると容量が空く）。"
                    "レポートのフォルダもゴミ箱に入れるのは with_report=true（利用者が言ったときだけ）。取得中・取り足し中は先に cancel_analysis。"
                    "どれが大きいかは info の about=storage。analysis_id は分析 ID か曲名。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True,
                                    open_world_hint=False),
    )
    async def archive_analysis(ctx: Context, analysis_id: str, restore: bool = False, delete: bool = False,
                               with_report: bool = False) -> str:
        if delete:
            return _call(runner.archive_analysis, _user(ctx), analysis_id, False, True, bool(with_report))["text"]
        return _call(runner.archive_analysis, _user(ctx), analysis_id, bool(restore))["text"]

    @mcp.tool(
        title="章立てを変える",
        description="レポートの章立てを、利用者の頼みどおりに変える（章を足す・外す・並べ替える）。**完成の前でも後でも使える**。"
                    "利用者が「音楽の章はいらない」「ファン心理の章を足して」「結果の章を分岐点の前に」「界隈ごとの章立てにして」のように頼んだときに使う。"
                    "章の id は read の chapters（intro＝冒頭・path＝拡大経路の章のまとまり・branch＝分岐点・music＝音楽・result＝結果・足した章は c1…）。"
                    "remove は外す章の id、add は足す章 [{\"title\": \"章の題\", \"about\": \"どんな章か（利用者の言葉）\", \"after\": \"<この章の後ろ>\"}]、"
                    "order は並べ替えたあとの id の並び（省いた章は後ろに）。instruction は利用者の言葉そのまま。"
                    "書く前なら章立てを変えるだけ（最初からその章立てで書く）。書いたあとは構成案を直して、足した章・冒頭・話がずれる章を書き直す"
                    "（返事に見込みが出るので伝え、確かめを求めずに next_task で進める）。前の版は残る。外した章も消さない。"
                    "章の中身だけの直しは revise。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                    open_world_hint=False),
    )
    async def restructure(ctx: Context, instruction: str = "", analysis_id: str | None = None, remove: list[str] | None = None,
                          add: list[dict] | None = None, order: list[str] | None = None) -> str:
        return _call(runner.restructure, _user(ctx), analysis_id, remove or [], add or [], order or [], instruction)["text"]

    @mcp.tool(
        title="動画を外す・戻す・足す",
        description="分析に使う動画を、利用者の頼みどおりに外す・戻す・足す。**完成の前でも後でも使える**。"
                    "action: exclude（「この動画は関係ないから外して」。以後の分析・本数・記事に使わない）／include（外した動画を戻す）／"
                    "add（「この動画も入れて」。Mac の UGC Analyzer が属性とコメントを取り、ラベルを付けて、関わるところを書き直す）。"
                    "targets は動画の URL（https://www.tiktok.com/@…/video/…）か番号（seq）。足す（add）は URL で。instruction は利用者の言葉そのまま。"
                    "外す・戻すはすぐ効き、書いてある章に関わるときは、関わる界隈のコメント分析と章を直す（返事のとおり next_task で進める）。"
                    "足すときは Mac が取得するので、返事を伝えて止まる（終わると Mac の通知。利用者が「〇〇の分析を続けて」と言ったら next_task）。"
                    "動画がこの分析に入っているかは info の about=video で確かめられる。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                    open_world_hint=True),
    )
    async def videos(ctx: Context, action: str, targets: list[str], instruction: str = "", analysis_id: str | None = None) -> str:
        return _waiting_text(ctx, _call(runner.videos, _user(ctx), analysis_id, action, targets, instruction))

    @mcp.tool(
        title="分析を比べる",
        description="2つ以上の分析を比べたレポートを書く。利用者が「前回からどれだけ伸びた？」「前の分析と比べて」「〇〇と△△を比べて」と頼んだときに使う。"
                    "analysis_ids は比べる分析の ID か曲名（2つ以上）。曲名を1つだけ渡すと、同じ曲の前回と今回を比べる"
                    "（比べる相手が無ければ、最新の数字で集め直す道が返るので、利用者に伝える）。instruction は利用者の言葉そのまま。"
                    "受け付けたら next_task で進める。できたレポートはレポートのフォルダの「比較」に置く。"
                    "数字だけ知りたいときは、レポートを書かずに info の overview を2つ読んで答えてもよい。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                    open_world_hint=False),
    )
    async def compare(ctx: Context, analysis_ids: list[str], instruction: str = "") -> str:
        return _call(runner.compare, _user(ctx), analysis_ids, instruction)["text"]

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

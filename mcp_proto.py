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
from pathlib import Path

import proto_runner as runner

logger = logging.getLogger("mcp")

PREFIX = "/mcp/"
USERS_FILE = Path(os.environ.get("UGC_MCP_USERS", Path.home() / "ugc-secrets" / "mcp_users.json"))

INSTRUCTIONS = (
    "UGC Analyzer（TikTok の楽曲 UGC 分析サービス）への接続です。分析の手順・指示書・検査はサービスが持っていて、"
    "あなたは「次の仕事を聞く → 指示書どおりにやる → 返す」を繰り返す係です。\n"
    + runner.REPEAT_RULE
    + "\n分析 ID が分からなければ、曲名をそのまま渡すか、status で一覧を見る。"
    + "\nレポートの完成後に利用者が直しを頼んだら revise（このレポートだけ）。今後ずっと続く指示の変更は settings"
    "（利用者が頼んだときだけ使う）。"
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

    def _call(fn, *args):
        try:
            return fn(*args)
        except runner.RunnerError as e:
            raise ToolError(str(e)) from e
        except Exception as e:
            logger.exception("道具の実行で例外: %s", fn.__name__)
            raise ToolError(f"サービス側のエラーです（{type(e).__name__}）。少し待って同じ道具を呼び直してください") from e

    rule = "\n\n繰り返し方: " + runner.REPEAT_RULE

    @mcp.tool(
        title="分析を始める",
        description=("TikTok の楽曲の UGC 分析を始める（取得を待ち行列に入れる）。利用者が「〇〇を分析して」と頼んだときに使う。"
                     "song は曲名、artist はアーティスト名。music_url は TikTok の楽曲ページ（https://www.tiktok.com/music/…）。"
                     + ("music_url（楽曲ページ）は、先にウェブ検索で探して付ける。同じ曲の楽曲ページが複数見つかったら（配信版・先行版・sped up 版など）、"
                        "全部を candidate_urls に入れる（サービスが UGC 数を比べ、一番使われているページと、その2割以上使われている同じ曲の公式のページを合わせて取る）。"
                        "1つしか見つからないときは、検索を変えてほかのページが無いか確かめてから、only_one=true を付けて渡す。"
                        "利用者が楽曲ページの URL を渡したときは、探し直さずに全部を music_urls に入れる（2つ以上なら全部から取って合わせる）。"
                        "楽曲ページが見つからなければ、その曲を使った TikTok の動画の URL を"
                        "video_url に付けてもよい（サービスが動画から楽曲ページを読み取る）。どちらも無しで呼ぶと、探し方が返ってくるだけで取得は始まらない。"
                        "利用者に URL を頼むのは、何通りも検索して見つからないときの最後の手段。利用者に確認は求めない。"
                        "取得を始めると、どの楽曲ページで進めるか（題・作者・UGC 数・URL）が返ってくるので、それを利用者に伝える。"
                        "取得は利用者の Mac の取得アプリが半日ほどかけてやり、終わると Mac の通知が出る。" if local else
                        "分かれば music_url。取得は半日ほどかかる。")
                     + "返ってきた内容を利用者に短く伝えて止まる（取得を待たない・見に来ない）。" + rule),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                    open_world_hint=True),
    )
    async def start_analysis(ctx: Context, song: str, artist: str = "", music_url: str | None = None,
                             video_url: str | None = None, candidate_urls: list[str] | None = None,
                             only_one: bool = False, music_urls: list[str] | None = None) -> str:
        return _call(runner.start_analysis, _user(ctx), song, artist, music_url or "", video_url or "",
                     candidate_urls or [], only_one, music_urls or [])["text"]

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
                    "wait=取得中なので止まる／done=完了なので止まる。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                    open_world_hint=False),
    )
    async def next_task(ctx: Context, analysis_id: str | None = None) -> str:
        return _call(runner.next_task, _user(ctx), analysis_id)["text"]

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
                    "analysis_id は分析 ID か曲名。返ってきた内容を利用者に短く伝える。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                    open_world_hint=True),
    )
    async def restart_analysis(ctx: Context, music_url: str | None = None, analysis_id: str | None = None,
                               music_urls: list[str] | None = None) -> str:
        return _call(runner.restart_analysis, _user(ctx), analysis_id, music_url or "", music_urls or [])["text"]

    @mcp.tool(
        title="取得をやめる",
        description="**利用者が「〇〇の取得をやめて」と頼んだときだけ使う**（やり直しまで頼まれたら restart_analysis）。"
                    "取得中・順番待ちの分析の取得をやめる。analysis_id は分析 ID か曲名。" + rule,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True,
                                    open_world_hint=False),
    )
    async def cancel_analysis(ctx: Context, analysis_id: str | None = None) -> str:
        return _call(runner.cancel_analysis, _user(ctx), analysis_id)["text"]

    @mcp.tool(
        title="知識ベースを新しくする",
        description="著者（山本慶太朗）の note の新しい記事を確かめ、知識ベースに取り込む準備をする。"
                    "利用者が「知識ベースを更新して」と頼んだときに使う（取得アプリも週1回、自動で確かめている）。"
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

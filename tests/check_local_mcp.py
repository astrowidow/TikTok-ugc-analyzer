#!/usr/bin/env python3
"""取得アプリの中の Claude の道具（collector_app/mcp_local.py）を、Claude と同じ形（標準入出力）で起こして確かめる。
TikTok には触らない（メニューバーのアプリも起こさない）。note には1回だけ一覧を聞く（--no-network で省く）。

  python tests/check_local_mcp.py                     … ソースから（collector/.venv の python で）
  python tests/check_local_mcp.py --app "/Applications/UGC Collector.app"   … 固めたアプリで
  python tests/check_local_mcp.py --analysis <分析フォルダ>   … W1 の確かめに使う分析（取得済みのもの）を写して使う

確かめること:
  1. 初期化・道具9つ・instructions
  2. status / start_analysis（URL が無ければ探し方を返す。URL があればそのまま始め、どの楽曲ページで進めるかを返す。この Mac の取得の設定・ポート 9250）
  3. W1 の next_task（編集できる指示書から組み立てる）・read のシート画像
  4. 指示書の道具: 一覧・全文・使えない書き換えは断る・使える書き換えは使われる・初期に戻す
  5. 知識ベース: 取り込み待ちの記事があると next_task が取り込みの仕事を先に渡す・差し戻し・受け取り・用語集に追記が付く
  6. update_knowledge（note の一覧を1回だけ見る）
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parent.parent
REAL_HOME = Path.home() / "Library" / "Application Support" / "UGC Collector"


def text_of(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content if getattr(c, "type", "") == "text")


def ok(cond: bool, what: str):
    print(("  OK  " if cond else "  NG  ") + what, flush=True)
    if not cond:
        ok.failed += 1


ok.failed = 0


def default_analysis():
    """W1 の確かめに使う取得済みの分析（運営の Mac のちょいとり）"""
    for d in sorted((REAL_HOME / "analyses").glob("*")):
        m = json.loads((d / "analysis.json").read_text(encoding="utf-8")) if (d / "analysis.json").exists() else {}
        if (m.get("acquisition") or {}).get("status") == "done" and (d / "derived" / "llm_input" / "records.jsonl").exists():
            return d
    return None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", help="固めたアプリ（.app）。省くとソースから")
    ap.add_argument("--analysis", help="W1 の確かめに写す分析フォルダ")
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--keep", action="store_true", help="試験の作業場を消さない")
    args = ap.parse_args()

    home = Path(tempfile.mkdtemp(prefix="ugc-mcp-check-"))
    env = {"UGC_COLLECTOR_HOME": str(home), "UGC_COLLECTOR_NO_APP_LAUNCH": "1", "UGC_COLLECTOR_NO_INSPECT": "1",
           "UGC_TEST_COUNTS": json.dumps({"7000000000000000011": 1632, "7000000000000000012": 31200, "7000000000000000013": 17700}),
           # 動画 → 音源（公式アカウントの動画は先行版 …11、一般の人の動画は多くが …12、sped up 版 …13、個人の音源 …99）
           "UGC_TEST_VIDEO_MUSIC": json.dumps({
               "7100000000000000001": ["7000000000000000011", "くらべる", "だれか"],
               "7100000000000000002": ["7000000000000000012", "くらべる", "だれか"],
               "7100000000000000003": ["7000000000000000012", "くらべる", "だれか"],
               "7100000000000000004": ["7000000000000000012", "くらべる", "だれか"],
               "7100000000000000005": ["7000000000000000013", "くらべる (sped up)", "だれか"],
               "7100000000000000006": ["7000000000000000099", "オリジナル楽曲 - someone", "someone"]}),
           # TikTok の discover のページの人気の動画の音源（「さがす」だけ）
           "UGC_TEST_DISCOVER": json.dumps({"さがす": [
               ["7000000000000000099", "オリジナル楽曲 - someone", "someone"], ["7000000000000000012", "さがす", "だれか"],
               ["7000000000000000013", "さがす", "だれか"], ["7000000000000000012", "さがす", "だれか"],
               ["7000000000000000098", "オリジナル楽曲 - other", "other"], ["7000000000000000012", "さがす", "だれか"]]}),
           "PATH": os.environ.get("PATH", ""),
           "HOME": os.environ.get("HOME", "")}
    if args.app:
        exe = str(Path(args.app) / "Contents" / "MacOS" / "UGC Collector")
        params = StdioServerParameters(command=exe, args=["--mcp"], env=env)
    else:
        env["PYTHONPATH"] = str(ROOT / "collector")
        params = StdioServerParameters(command=sys.executable, args=["-m", "collector_app", "--mcp"], env=env,
                                       cwd=str(ROOT))
    src = Path(args.analysis) if args.analysis else default_analysis()
    aid = None
    if src:
        aid = "w1check-" + src.name
        dst = home / "analyses" / aid
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("covers", "state", "outputs", "eval"))
        m = json.loads((dst / "analysis.json").read_text(encoding="utf-8"))
        m.update({"analysis_id": aid, "owner": "local", "title": "W1確かめ " + str(m.get("title", ""))[:20]})
        m.pop("proto", None)
        (dst / "analysis.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    print("作業場:", home, "／ W1 の分析:", src or "なし")

    try:
        async with Client(params, read_timeout_seconds=120) as c:
            print("[1] 初期化・道具・instructions")
            tools = sorted(t.name for t in (await c.list_tools()).tools)
            ok(tools == ["cancel_analysis", "next_task", "prompts", "read", "restart_analysis", "revise", "settings",
                         "start_analysis", "status", "submit", "update_knowledge"], f"道具 {tools}")
            ok("next_task" in (c.instructions or ""), "instructions が返る")

            print("[2] status と start_analysis")
            r = await c.call_tool("status", {})
            ok(not r.is_error, "status: " + text_of(r).splitlines()[0][:80])
            url = "https://www.tiktok.com/music/%E3%81%A6%E3%81%99%E3%81%A8-7000000000000000001"
            r = await c.call_tool("start_analysis", {"song": "てすと", "artist": "だれか"})
            ok(not r.is_error and "まだ取得を始めていない" in text_of(r) and "ウェブ検索" in text_of(r)
               and not list((home / "analyses").glob("a*")), "URL が無いと、AI に探し方を返す（利用者には聞かない）")
            ok("site:tiktok.com/music" in text_of(r) and "video_url" in text_of(r) and "最後の手段" in text_of(r),
               "探し方に、検索の言葉の候補・動画からの道・最後の手段が書いてある")
            r = await c.call_tool("start_analysis", {"song": "べつのきょく", "artist": "だれか",
                                                     "video_url": "https://www.tiktok.com/@someone/video/7000000000000000001"})
            ok("頼まれた曲「べつのきょく」と違う" in text_of(r) and not list((home / "analyses").glob("a*")),
               "動画の音源が頼まれた曲と違えば、始めずに別の動画を探すよう返す")
            r = await c.call_tool("start_analysis", {"song": "てすと", "artist": "だれか", "only_one": True,
                                                     "video_url": "https://www.tiktok.com/@someone/video/7000000000000000001"})
            tv = text_of(r)
            mv = re.search(r"分析 ID: (a[0-9-]+[0-9a-f]+)", tv)
            ok("動画" in tv and "音源から楽曲ページを見つけた" in tv and mv, "動画の URL から楽曲ページを見つけて始める")
            if mv:
                shutil.rmtree(home / "analyses" / mv.group(1), ignore_errors=True)
            cu = [f"https://www.tiktok.com/music/x-70000000000000000{n}" for n in (11, 12, 13)]
            r = await c.call_tool("start_analysis", {"song": "くらべる", "artist": "だれか", "candidate_urls": cu})
            tc = text_of(r)
            mc = re.search(r"分析 ID: (a[0-9-]+[0-9a-f]+)", tc)
            meta_c = json.loads((home / "analyses" / mc.group(1) / "analysis.json").read_text(encoding="utf-8")) if mc else {}
            chosen = meta_c.get("music_url") or ""
            dropped_part = tc.split("外した楽曲ページ", 1)[-1] if "外した楽曲ページ" in tc else ""
            ok(chosen == cu[1] and meta_c.get("music_urls") == [cu[1], cu[2]] and "3 つを比べ" in tc
               and "合わせて進めます" in tc and cu[0] in dropped_part and "20%未満" in dropped_part and "それも入れて" in tc,
               f"楽曲ページの候補を比べ、一番使われているもの（UGC 31200）と、その20%以上（17700）を合わせて取る。"
               f"少ないもの（1632）は外したと伝える（主: {chosen[-6:]}）")
            if mc:
                rec = json.loads((home / "analyses" / mc.group(1) / "raw" / "music_pages.json").read_text(encoding="utf-8"))
                ok([x.get("video_count") for x in rec] == [31200, 17700] and "合わせて約48,900本" in tc,
                   "受け付けのときに、ページごとの UGC 数を残し、合計を伝える")
                r = await c.call_tool("restart_analysis", {"analysis_id": mc.group(1), "music_urls": cu})
                tr = text_of(r)
                mr = re.search(r"分析 ID: (a[0-9-]+[0-9a-f]+)", tr)
                meta_r = json.loads((home / "analyses" / mr.group(1) / "analysis.json").read_text(encoding="utf-8")) if mr else {}
                ok(not r.is_error and meta_r.get("music_urls") == [cu[1], cu[2], cu[0]] and "外した楽曲ページ" not in tr
                   and "渡された楽曲ページ 3 つを全部合わせて取る" in tr,
                   "「それも入れて」: 渡した楽曲ページは全部合わせて取り直す（主は一番使われているもの）")
                for x in (mc.group(1), mr.group(1) if mr else None):
                    if x:
                        shutil.rmtree(home / "analyses" / x, ignore_errors=True)
            vu = [f"https://www.tiktok.com/@u{n}/video/710000000000000000{n}" for n in range(1, 7)]
            r = await c.call_tool("start_analysis", {"song": "くらべる", "artist": "だれか", "video_urls": vu[:1]})
            ok("一般の人の動画" in text_of(r) and "あと 2 本以上" in text_of(r) and not list((home / "analyses").glob("a*")),
               "動画1本（公式アカウント）から楽曲ページが1つだけなら、始めずに一般の人の動画を足させる")
            r = await c.call_tool("start_analysis", {"song": "くらべる", "artist": "だれか", "video_urls": vu})
            tv2 = text_of(r)
            mv2 = re.search(r"分析 ID: (a[0-9-]+[0-9a-f]+)", tv2)
            meta_v = json.loads((home / "analyses" / mv2.group(1) / "analysis.json").read_text(encoding="utf-8")) if mv2 else {}
            ok([u.rsplit("-", 1)[-1] for u in meta_v.get("music_urls", [])] == ["7000000000000000012", "7000000000000000013"]
               and "動画 6 本の音源などから見つけた楽曲ページ 3 つを比べ" in tv2 and "7000000000000000011" in tv2.split("外した楽曲ページ", 1)[-1]
               and "7000000000000000099" not in tv2,
               "動画6本の音源から楽曲ページを見つけて比べ、31200 と 17700 を合わせて取る（先行版 1632 は外す・個人の音源は候補にしない）")
            if mv2:
                shutil.rmtree(home / "analyses" / mv2.group(1), ignore_errors=True)
            r = await c.call_tool("start_analysis", {"song": "さがす", "artist": "だれか"})
            ts = text_of(r)
            ms = re.search(r"分析 ID: (a[0-9-]+[0-9a-f]+)", ts)
            meta_s = json.loads((home / "analyses" / ms.group(1) / "analysis.json").read_text(encoding="utf-8")) if ms else {}
            ok([u.rsplit("-", 1)[-1] for u in meta_s.get("music_urls", [])] == ["7000000000000000012", "7000000000000000013"]
               and "TikTok で人気の動画など 6 本の音源から見つけた楽曲ページ 2 つを比べ" in ts and "7000000000000000099" not in ts,
               "曲名だけで、Mac が TikTok の人気の動画の音源から楽曲ページを見つけ、31200 と 17700 を合わせて取る（個人の音源は候補にしない）")
            if ms:
                shutil.rmtree(home / "analyses" / ms.group(1), ignore_errors=True)
            r = await c.call_tool("start_analysis", {"song": "", "music_url": url})
            ok("1つだけ渡された" in text_of(r) and "only_one=true" in text_of(r) and not list((home / "analyses").glob("a*")),
               "楽曲ページが1つだけなら、始めずにほかの版を探し直させる")
            r = await c.call_tool("start_analysis", {"song": "", "music_url": url, "only_one": True})
            t = text_of(r)
            ok("次の楽曲ページで進めます" in t and url in t, "ほかに無ければ（only_one）そのまま始め、どの楽曲ページで進めるかを返す")
            ok(not r.is_error and "あなたの Mac" in t and "メール" not in t, "start_analysis が Mac 向けの文面で受け付ける")
            m_aid = re.search(r"分析 ID: (a[0-9-]+[0-9a-f]+)", t)
            ok(m_aid is not None, "分析 ID が返る")
            if m_aid:
                meta = json.loads((home / "analyses" / m_aid.group(1) / "analysis.json").read_text(encoding="utf-8"))
                ok(meta["acquisition"]["status"] == "queued" and meta["acquisition_settings"]["chrome_port"] == "9250",
                   "この Mac の取得の設定（ポート 9250）で待ち行列に入る")
                ok(meta["title"] == "てすと", f"URL だけで頼むと題名は曲名の部分（{meta['title']}）")
                r = await c.call_tool("status", {"analysis_id": m_aid.group(1)})
                ok("取得アプリが動いていません" in text_of(r), "取得アプリが止まっていると status がそう言う")
                url2 = "https://www.tiktok.com/music/%E3%81%A6%E3%81%99%E3%81%A8-7000000000000000002"
                r = await c.call_tool("restart_analysis", {"analysis_id": m_aid.group(1), "music_url": url2})
                t2 = text_of(r)
                old = json.loads((home / "analyses" / m_aid.group(1) / "analysis.json").read_text(encoding="utf-8"))
                m2 = re.search(r"分析 ID: (a[0-9-]+[0-9a-f]+)", t2)
                ok(not r.is_error and "前の取得" in t2 and url2 in t2 and old["acquisition"]["status"] == "cancelled"
                   and (home / "locks" / f"cancel-{m_aid.group(1)}").exists() and m2 and m2.group(1) != m_aid.group(1),
                   "取得をやめて別の楽曲ページでやり直せる（前の分析はやめた・止める印・新しい分析）")
                if m2:   # 最初からやり直す（URL なし）: やめて、探し方を返す
                    r = await c.call_tool("restart_analysis", {"analysis_id": m2.group(1)})
                    st2 = json.loads((home / "analyses" / m2.group(1) / "analysis.json").read_text(encoding="utf-8"))
                    ok("最初にやり直す" in text_of(r) and "site:tiktok.com/music" in text_of(r)
                       and st2["acquisition"]["status"] == "cancelled", "URL なしのやり直しは、やめて楽曲ページ探しから")
                for x in (m_aid.group(1), m2.group(1) if m2 else None):
                    if x:
                        shutil.rmtree(home / "analyses" / x, ignore_errors=True)

            if aid:
                print("[3] W1 の next_task と read")
                r = await c.call_tool("next_task", {"analysis_id": aid})
                t = text_of(r)
                m = re.search(r"task_id: `([^`]+)`", t)
                ok(not r.is_error and m and "axes" in m.group(1), f"W1 の最初の仕事（{m.group(1) if m else t[:120]}）")
                sheets = re.findall(r"sheet:\d+", t)
                if sheets:
                    r2 = await c.call_tool("read", {"name": sheets[0], "analysis_id": aid})
                    ok(any(getattr(x, "type", "") == "image" for x in r2.content), f"read {sheets[0]} で画像が返る")

            print("[4] 指示書の道具")
            r = await c.call_tool("prompts", {"action": "list"})
            ok("label.md" in text_of(r) and "編集済み" not in text_of(r), "一覧（まだ編集なし）")
            r = await c.call_tool("prompts", {"action": "get", "name": "axes"})
            full = text_of(r).split("````markdown\n", 1)[-1].rsplit("````", 1)[0]
            ok("{{song}}" in full, f"全文が読める（{len(full)}字）")
            r = await c.call_tool("prompts", {"action": "set", "name": "axes", "text": full.replace("{{song}}", "")})
            ok("書き換えませんでした" in text_of(r), "差し込みの印を消した書き換えは断る")
            marker = "【試験の印: 界隈は少なめに】"
            r = await c.call_tool("prompts", {"action": "set", "name": "axes", "text": marker + "\n" + full})
            ok("書き換えました" in text_of(r), "使える書き換えは受け付ける")
            if aid:
                st = home / "analyses" / aid / "state" / "tasks.json"
                s = json.loads(st.read_text(encoding="utf-8"))
                for x in s["tasks"]:
                    if x["type"] == "axes":
                        x["status"] = "pending"
                st.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
                r = await c.call_tool("next_task", {"analysis_id": aid})
                ok(marker in text_of(r), "編集した指示書が次の仕事に使われる")
            r = await c.call_tool("prompts", {"action": "reset", "name": "axes"})
            ok("初期の指示書に戻しました" in text_of(r), "初期に戻せる")

            print("[5] 知識ベースの取り込み（試験の記事を1本、取り込み待ちにする）")
            kb = home / "knowledge"
            key = "ntest0000check"
            fname = f"2099-01-01_{key}.md"
            (kb / "notes" / fname).write_text("# 試験の記事\n\nhttps://note.com/x/n/" + key + "\n公開: 2099-01-01\n\n"
                                              "この記事は試験です。ゴリラ界隈という新しい界隈が出てきた。\n", encoding="utf-8")
            idx = json.loads((kb / "INDEX.json").read_text(encoding="utf-8"))
            idx.append({"key": key, "name": "試験の記事", "date": "2099-01-01", "file": fname, "price": 0, "canRead": True})
            (kb / "INDEX.json").write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
            (kb / "update_state.json").write_text(json.dumps({"pending": [key], "last_check": "2099-01-01T00:00:00+09:00"}),
                                                  encoding="utf-8")
            if aid:   # 界隈の確認の前（利用者が答えを待つ）には、はさまない
                r = await c.call_tool("next_task", {"analysis_id": aid})
                ok("kb/" not in text_of(r), "界隈の確認より前には取り込みをはさまない")
                st = home / "analyses" / aid / "state" / "tasks.json"
                s = json.loads(st.read_text(encoding="utf-8"))
                for x in s["tasks"]:
                    if x["kind"] == "ask_user":
                        x["status"] = "done"
                st.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
            r = await c.call_tool("next_task", {"analysis_id": aid} if aid else {})
            t = text_of(r)
            ok(f"kb/card-{key}" in t and "ゴリラ界隈" in t, "取り込みの仕事が先に渡される（記事の本文つき）")
            r = await c.call_tool("submit", {"task_id": f"kb/card-{key}", "output": '{"card": {"title": "x"}}'})
            ok('"ok": false' in text_of(r), "形の悪いカードは差し戻す")
            good = {"card": {"title": "試験", "kind": "コラム", "song": None, "artist": None, "platform": "TikTok",
                             "buzz_type": None, "pathway": "なし", "communities": ["ゴリラ界隈"], "formats": [],
                             "why_claims": [], "reproducible": [], "evidence_style": "試験", "coined_terms": ["ゴリラ界隈"],
                             "numbers": "", "reusable_insight": "試験"},
                    "glossary": [{"section": "D", "term": "ゴリラ界隈", "text": "試験の語"}]}
            r = await c.call_tool("submit", {"task_id": f"kb/card-{key}", "output": json.dumps(good, ensure_ascii=False)})
            ok('"ok": true' in text_of(r), "正しい形は受け取る")
            cards = (kb / "distilled" / "cards.jsonl").read_text(encoding="utf-8").splitlines()
            ok(fname in cards[-1] and len(cards) == 83, f"カードが1行足される（{len(cards)}行）")
            if aid:
                r = await c.call_tool("read", {"name": "kb:glossary", "analysis_id": aid, "page": 99})
                pages = re.findall(r"page=1〜(\d+)", text_of(r))
                last = max([int(p) for p in pages] or [1])
                r = await c.call_tool("read", {"name": "kb:glossary", "analysis_id": aid, "page": last})
                ok("ゴリラ界隈" in text_of(r), f"kb:glossary の最後のページ（{last}）に追記が付く")
                r = await c.call_tool("next_task", {"analysis_id": aid})
                ok("kb/" not in text_of(r), "取り込みが済んだら分析の仕事に戻る")

            if not args.no_network:
                print("[6] update_knowledge（note の一覧を見る）")
                r = await c.call_tool("update_knowledge", {})
                ok(not r.is_error and "著者の note を確かめました" in text_of(r), text_of(r).splitlines()[0][:80])
    finally:
        if not args.keep:
            shutil.rmtree(home, ignore_errors=True)
    print("結果:", "全部 OK" if not ok.failed else f"NG {ok.failed} 件")
    return 1 if ok.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

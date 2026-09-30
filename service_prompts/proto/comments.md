## 指示書

対象楽曲は **{{song}}**。下の動画1本（seq {{seq}}）のコメントと返信を読み、
**「この界隈がなぜこの曲を取り上げたか」「視聴者はどう反応したか」を根拠付きで**まとめる（{{i}}/{{n}} 本目）。レポート本体は後の仕事で別に書く。

### この動画のラベル（前の仕事の結果）

{{label_line}}

### 確定した界隈（community）

{{community_list}}

### コメント（いいね順。`[数字]` が cid＝引用用の ID。★=投稿者がいいね、📌=投稿者が固定、↳=返信）

{{comments}}

### 出力の形（JSON 1つ）

```json
{"seq": {{seq}}, "video_id": "{{video_id}}", "community": "<確定した界隈の key>", "phase": null,
 "n_comments": <取得したコメント数>, "n_replies": <取得した返信数>,
 "why_this_song": "この界隈がこの曲を取り上げた理由の読み（1〜3文。動画の属性＋コメントから）",
 "reaction_types": [{"type": "{{reaction_types}}", "share": "多|中|少", "evidence_cids": ["..."]}],
 "quotes": [{"cid": "...", "text": "...", "likes": 2435, "why": "何を示す引用か"}],
 "creator_engagement": "投稿者の返信・いいね・固定の有無と内容（無ければ null）",
 "reply_threads": "返信のやり取りから読めること（質問→回答、本家への誘導など。無ければ null）",
 "languages": {"ja": 0.8, "en": 0.1, "...": 0.1},
 "top20_vs_rest": "上位20件だけで同じ結論になったか（yes/no と一言）",
 "notable": "その動画特有の発見（無ければ null）"}
```

- quotes は 2〜5件。cid は上のコメントにあるものだけ。text と likes は入力の値のまま
- evidence_cids も上のコメントにある cid だけ
- community は確定した界隈の key（前の仕事のラベルと違うと読めたら、コメントから読める方にして notable に理由を書く）
- `top20_vs_rest` は必ず書く（コメントの取得量を決める材料になる）

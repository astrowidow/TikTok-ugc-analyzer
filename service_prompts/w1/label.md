## 指示書

対象楽曲は **{{song}}**。下の {{n}} 本の動画に、確定した分類軸でラベルを付ける（{{batch}}/{{n_batches}} 回目）。

### 確定した分類軸

```json
{{taxonomy_json}}
```
{{settings}}
### 対象の動画（{{n}} 本: seq {{seq_list}}）

説明文だけで決めにくい動画は、「読む資料」のシートを read で見る（各動画の「サムネイル」欄に、どのシートのどの枠かが書いてある）。

{{entries}}

### 出力の形（TSV）

タブ区切り、ヘッダ付き、{{n}} 行:

```
seq	community	format	motive	tier	conf	reason
```

- community / format / motive / tier は、確定した分類軸の key から選ぶ（当てはまらなければ `unknown`）
- conf は H / M / L（H = 複数のシグナルが一致、M = 1つのシグナル、L = 推測）
- reason は日本語で1行（根拠にしたシグナル。例: 「bio に『○○所属 16歳』、#今日好き」）。サムネイルを根拠にした場合は「サムネ:」と書く。reason の中にタブを入れない
- 上の {{n}} 本すべてを1回ずつ。ほかの seq は入れない。地域はサービスが付けるので列に入れない

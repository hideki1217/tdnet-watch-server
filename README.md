# TDNet Watch Servers

- `tdnet_move_into_the_black`
    - TDnet(適時開示)を監視し、決算短信から「黒字転換」(前期 < 0 かつ 当期 > 0)した会社を列挙する
    - やのしんTDnet WebAPIから当日の開示一覧を取得し、添付XBRL(iXBRL)のサマリーから営業利益・経常利益・親会社株主に帰属する当期純利益を読む
    - 処理済みはSQLite(`.cache/tdnet_seen.sqlite3`)で管理し、重複通知を防ぐ
    - 環境変数 `TDNET_MOVE_INTO_THE_BLACK_WEBHOOK_URL` を設定すると Slack / Discord へ通知

    ```sh
    uv run tdnet_move_into_the_black.py            # 常駐して監視(60秒間隔)
    uv run tdnet_move_into_the_black.py --once     # 1回だけ実行
    uv run tdnet_move_into_the_black.py --date 20260515 --once   # 指定日を処理
    ```

    Docker Compose でも起動できる(`.env` の `TDNET_MOVE_INTO_THE_BLACK_WEBHOOK_URL` を読み込み、`.cache` を永続化):

    ```sh
    docker compose up -d --build
    docker compose logs -f
    ```

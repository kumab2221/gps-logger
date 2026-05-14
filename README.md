# gps-logger

Raspberry Pi 5 と GPS TTL モジュールで、ネットワークなし・ディスプレイなしの状態でも生 NMEA ログを保存するためのオフライン GPS ロガーです。

動作確認では、セットアップ手順に沿って `/var/log/gps-logger/` 配下に期待する NMEA ログ、TSV ログ、`latest_status.json` を取得できています。

## 方針

- GPS モジュールから出力される生 NMEA をそのまま保存する
- Raspberry Pi 側時刻と起動後秒数を TSV に併記する
- systemd で起動時に自動開始する
- 一定間隔で `fsync` し、突然の電源断でも直近数秒以外を残しやすくする
- 解析、地図表示、DB 保存は現地測定後に別工程で行う

## ドキュメント

| ファイル | 内容 |
|---|---|
| [gps_logger.py](gps_logger.py) | GPS ロガー本体 |
| [doc/gps-logger-setup.md](doc/gps-logger-setup.md) | Raspberry Pi 側の構築、開発、運用、ログ回収 |
| [doc/VK2828U7G5LF_GPSモジュール.md](doc/VK2828U7G5LF_GPSモジュール.md) | VK2828U7G5LF GPS モジュールの配線、ピン説明、更新レート設定 |

## ログ出力

ロガーは `/var/log/gps-logger/` にセッション単位でログを保存します。

```text
/var/log/gps-logger/
├── latest_status.json
└── session_YYYYMMDD_HHMMSS_bootid/
    ├── session_meta.json
    ├── part_0000.nmea
    └── part_0000.tsv
```

| ファイル | 内容 |
|---|---|
| `part_*.nmea` | GPS モジュールから受信した生 NMEA 文 |
| `part_*.tsv` | `host_time`, `monotonic_sec`, `nmea` を記録した確認用ログ |
| `latest_status.json` | 最新の受信状態、チェックサム数、fix 状態など |
| `session_meta.json` | セッション開始時刻、boot ID など |

## 最短の確認コマンド

```bash
sudo systemctl status gps-logger
sudo cat /var/log/gps-logger/latest_status.json
sudo find /var/log/gps-logger -type f -printf "%p %s bytes\n"
```

測位できているかは、主に以下を見ます。

| 項目 | 測位前 | 測位後 |
|---|---|---|
| `GPRMC` | `V` | `A` |
| `GPGGA` fix 品質 | `0` | `1` 以上 |
| `GPGGA` 衛星数 | `00` | `03` 以上、安定運用では `06` 以上が目安 |

## 基本構成

- Raspberry Pi 5
- VK2828U7G5LF GPS モジュール
- GPS アンテナ
- モバイルバッテリー
- microSD カード
- ジャンパーワイヤ

通常のログ取得では GPS の `TX` を Raspberry Pi の `RXD` に接続し、GPS の `RX` は未接続にします。モジュール設定を書き換える場合だけ、GPS `RX` と Raspberry Pi `TXD` を接続します。

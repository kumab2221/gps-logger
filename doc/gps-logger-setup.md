# Raspberry Pi 5 オフライン GPS ロガー構築・運用手順

Raspberry Pi 5 と GPS TTL モジュールを使い、屋外で生 NMEA ログを保存するための手順。

この手順で、`/var/log/gps-logger/` 配下に期待する `.nmea`、`.tsv`、`latest_status.json` を取得できることを確認済み。

## 目的

測定中は以下の制約がある前提にする。

- ディスプレイなしでも動作する
- ディスプレイが接続されていればログ状況を表示する
- ネットワーク接続なし
- バッテリー駆動
- 予期せず電源が落ちる可能性あり
- 測定後にログを回収して解析する

そのため、現地では解析よりも **GPS から出力される生 NMEA データを確実に保存すること** を優先する。

```text
Raspberry Pi 起動
  ↓
systemd により GPS ロガーを自動起動
  ↓
/dev/ttyAMA0 から NMEA を読み続ける
  ↓
/var/log/gps-logger/ にログ保存
  ↓
ディスプレイ接続時は /dev/tty1 に最新状態を表示
  ↓
一定秒ごとに flush + fsync
  ↓
突然電源断しても、直近数秒以外のログを残しやすくする
```

## ファイル構成

| ファイル | 内容 |
|---|---|
| `gps_logger.py` | GPS ロガー本体 |
| `doc/gps-logger-setup.md` | Raspberry Pi 側の構築、開発、運用手順 |
| `doc/VK2828U7G5LF_GPSモジュール.md` | GPS モジュールの配線、ピン、更新レート設定 |

Raspberry Pi 上では、運用用に `gps_logger.py` を `/opt/gps-logger/gps_logger.py` へ配置する。

このプロジェクトのデフォルトは、GPS モジュールが 10Hz / 115200bps に設定済みである前提。

| 項目 | デフォルト |
|---|---|
| UART ポート | `/dev/ttyAMA0` |
| ボーレート | `115200` |
| 想定更新レート | `10Hz` |

## 保存するログ

| ファイル | 内容 | 目的 |
|---|---|---|
| `part_*.nmea` | GPS モジュールから来た生 NMEA 文 | 後解析用の一次データ |
| `part_*.tsv` | Raspberry Pi 側時刻、起動後秒数、NMEA 文 | 測定時刻や欠損確認用 |
| `latest_status.json` | 最新の受信状態 | 動作確認、fix 状態確認 |
| `session_meta.json` | セッション開始情報 | 測定単位の識別 |
| 画面表示 | 最新状態と直近の NMEA 文 | ディスプレイ接続時の現地確認 |

`host_time` はネットワーク未接続時にズレている可能性があるため、解析時には `monotonic_sec` も併用する。

## 開発時の手順

ここでは、リポジトリ内の `gps_logger.py` を編集して動作確認し、問題なければ Raspberry Pi の運用場所へ反映する。

### 1. 構文確認

```bash
python3 -m py_compile gps_logger.py
```

### 2. Raspberry Pi 上で手動実行する

systemd に載せる前、またはスクリプト変更後は手動で確認する。

```bash
mkdir -p ./run/gps-logger
sudo ./gps_logger.py --out ./run/gps-logger --rotate-sec 60 --fsync-sec 2
```

別ターミナルでログを確認する。

```bash
sudo find ./run/gps-logger -type f -printf "%p %s bytes\n"
sudo cat ./run/gps-logger/latest_status.json
```

停止する場合は、実行中のターミナルで `Ctrl + C`。

期待するファイル構成:

```text
./run/gps-logger/latest_status.json
./run/gps-logger/session_YYYYMMDD_HHMMSS_bootid/session_meta.json
./run/gps-logger/session_YYYYMMDD_HHMMSS_bootid/part_0000.nmea
./run/gps-logger/session_YYYYMMDD_HHMMSS_bootid/part_0000.tsv
```

`total_lines` が増えていれば、GPS モジュールから NMEA を受信できている。

画面表示の内容をターミナル上で確認したい場合は、表示先に標準出力を指定する。

```bash
sudo ./gps_logger.py --out ./run/gps-logger --display always --display-tty -
```

### 3. 運用場所へ反映する

手動実行で問題なければ、運用用ディレクトリへインストールする。

```bash
sudo install -m 0755 gps_logger.py /opt/gps-logger/gps_logger.py
```

systemd で運用中の場合は再起動する。

```bash
sudo systemctl restart gps-logger
sudo systemctl status gps-logger
```

## 運用時の手順

Raspberry Pi を屋外へ持ち出してログを取るための初期設定と日々の操作。

### 1. ハードウェア接続

VK2828U7G5LF の詳細なピン説明は [VK2828U7G5LF_GPSモジュール.md](VK2828U7G5LF_GPSモジュール.md) を参照。

通常のログ取得では受信だけできればよいため、GPS モジュールの `RX` は接続しない。

| GPS モジュール | Raspberry Pi 5 |
|---|---|
| VCC | 3.3V |
| GND | GND |
| TX | GPIO15 / RXD |
| RX | 未接続 |

```text
GPS TX → Raspberry Pi RX
GPS RX → 未接続
```

GPS RX を Raspberry Pi の TX に接続すると、Raspberry Pi 側から文字列や制御データが GPS へ送られることがある。受信ロガーとして使うだけなら未接続の方が安全。

### 2. Raspberry Pi の UART 設定

```bash
sudo raspi-config
```

以下のように設定する。

```text
Interface Options
  → Serial Port
    → Login shell over serial? No
    → Serial hardware enabled? Yes
```

設定後、再起動する。

```bash
sudo reboot
```

再起動後、`/dev/ttyAMA0` が存在することを確認する。

```bash
ls -l /dev/ttyAMA0
```

例:

```text
/dev/ttyAMA0
```

### 3. gpsd を停止する

`gpsd` が動いていると GPS モジュールへコマンドを送る場合がある。生 NMEA をそのまま読む方針なので停止する。

```bash
sudo systemctl stop gpsd gpsd.socket 2>/dev/null
sudo systemctl disable gpsd gpsd.socket 2>/dev/null
```

確認する場合:

```bash
systemctl status gpsd
systemctl status gpsd.socket
```

### 4. ロガーをインストールする

```bash
sudo mkdir -p /opt/gps-logger
sudo mkdir -p /var/log/gps-logger
sudo install -m 0755 gps_logger.py /opt/gps-logger/gps_logger.py
```

| ディレクトリ | 用途 |
|---|---|
| `/opt/gps-logger` | GPS ロガープログラム置き場 |
| `/var/log/gps-logger` | GPS ログ保存先 |

### 5. systemd サービス化

```bash
sudo nano /etc/systemd/system/gps-logger.service
```

以下を貼り付ける。

```ini
[Unit]
Description=Offline GPS NMEA Logger
After=multi-user.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/gps-logger/gps_logger.py --out /var/log/gps-logger --rotate-sec 300 --fsync-sec 2
Restart=always
RestartSec=2
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

反映して起動する。

```bash
sudo systemctl daemon-reload
sudo systemctl enable gps-logger
sudo systemctl start gps-logger
sudo systemctl status gps-logger
```

正常なら以下のような表示になる。

```text
Active: active (running)
```

ログを見る場合:

```bash
sudo journalctl -u gps-logger -f
```

### 6. 運用前の動作確認

```bash
sudo systemctl restart gps-logger
sleep 10
sudo cat /var/log/gps-logger/latest_status.json
sudo find /var/log/gps-logger -type f -printf "%p %s bytes\n"
```

例:

```json
{
  "started_at": "2026-05-12T14:20:31.123456+09:00",
  "port": "/dev/ttyAMA0",
  "baud": 115200,
  "total_lines": 25,
  "checksum_ok": 25,
  "checksum_ng": 0,
  "last_update": "2026-05-12T14:20:45.123456+09:00",
  "last_raw": "$GPRMC,,V,,,,,,,,,,N*53",
  "last_sentence": "$GPRMC",
  "rmc_utc": "",
  "rmc_status": "V"
}
```

`total_lines` が増えていれば、GPS モジュールから NMEA を受信できている。

ディスプレイが接続されている場合は、`/dev/tty1` にライブ表示される。

表示される内容:

- 最新更新時刻
- 受信行数とチェックサム OK/NG 数
- RMC/GGA の測位状態
- 直近の NMEA 文

画面表示を明示的に無効化する場合は、systemd の `ExecStart` に `--display never` を追加する。

```ini
ExecStart=/usr/bin/python3 /opt/gps-logger/gps_logger.py --out /var/log/gps-logger --rotate-sec 300 --fsync-sec 2 --display never
```

ディスプレイ検出に関係なく強制表示する場合は、以下のようにする。

```ini
ExecStart=/usr/bin/python3 /opt/gps-logger/gps_logger.py --out /var/log/gps-logger --rotate-sec 300 --fsync-sec 2 --display always --display-tty /dev/tty1
```

### 7. 測位成功・失敗の見方

測位前によく見る状態:

```text
$GPRMC,,V,,,,,,,,,,N*53
$GPGGA,,,,,,0,00,99.99,,,,,,*48
```

| NMEA | 値 | 意味 |
|---|---|---|
| GPRMC | `V` | 無効 |
| GPGGA | `0` | fix なし |
| GPGGA | `00` | 使用衛星数 0 |
| GPGGA | `99.99` | HDOP 無効値 |

測位成功状態:

```text
$GPRMC,055012.00,A,3439.1234,N,13530.5678,E,0.12,85.4,120526,,,A*68
$GPGGA,055012.00,3439.1234,N,13530.5678,E,1,08,1.2,50.0,M,34.0,M,,*5A
```

| NMEA | 値 | 意味 |
|---|---|---|
| GPRMC | `A` | 有効 |
| GPGGA | `1` | GPS fix あり |
| GPGGA | `08` | 8 衛星使用 |
| GPGGA | `1.2` | HDOP 良好 |

最低限見る変化:

```text
GPRMC の V → A
GPGGA の fix 品質 0 → 1
GPGGA の衛星数 00 → 03 以上
```

安定測位を狙うなら、衛星数は `06` 以上ある方がよい。

### 8. 外で測定するときの運用

測定前:

```bash
sudo systemctl restart gps-logger
sleep 10
sudo cat /var/log/gps-logger/latest_status.json
```

測定時:

```text
1. GPS モジュールを Raspberry Pi に接続する
2. バッテリーを接続する
3. Raspberry Pi が起動する
4. gps-logger.service が自動起動する
5. GPS アンテナを空が見える方向に向ける
6. 5〜10分ほど静止して初回測位を待つ
7. 屋外を歩いて測定する
8. 測定後、可能なら安全にシャットダウンする
```

注意点:

- GPS アンテナは空に向ける
- 建物、金属、車体、PC 本体からなるべく離す
- 初回測位は数分かかることがある
- 起動直後すぐに歩き出すと、最初の数分は無効ログになる可能性がある
- 室内では基本的に測位できないと考える

可能なら以下で終了する。

```bash
sudo shutdown now
```

### 9. 電源断への対策

本ロガーでは以下を行う。

| 対策 | 内容 |
|---|---|
| `fsync` | 一定秒ごとに SD カードへ強制書き込み |
| ファイル分割 | 5分ごとにログファイルを分ける |
| systemd 自動再起動 | プロセス異常終了時に再起動 |
| 起動時自動開始 | 電源再投入後に自動でログ取得再開 |
| 生 NMEA 保存 | 解析処理失敗によるデータ欠損を防ぐ |

systemd の起動設定では `--fsync-sec 2` としている。突然電源が落ちた場合でも、理屈上は直近数秒以外のログは残りやすい。

守れないもの:

- 電源断の瞬間の最後の数秒
- SD カード自体の破損
- バッテリー電圧低下による不安定動作
- GPS アンテナ不良
- 屋内、ビル街での測位不能

### 10. ログ回収

測定後、Raspberry Pi にログインして確認する。

```bash
sudo find /var/log/gps-logger -type f -printf "%p %s bytes\n"
```

tar.gz でまとめる。

```bash
sudo tar -czf ~/gps_logs_$(date +%Y%m%d_%H%M%S).tar.gz /var/log/gps-logger
sudo chown $USER:$USER ~/gps_logs_*.tar.gz
```

PC 側から `scp` で取得する例:

```bash
scp kuma@raspberrypi.local:~/gps_logs_*.tar.gz .
scp kuma@192.168.1.100:~/gps_logs_*.tar.gz .
```

### 11. 解析時の基本確認

RMC を確認する。

```bash
grep GPRMC /var/log/gps-logger/session_*/part_*.nmea | head
```

`A` になっていれば測位成功。

GGA を確認する。

```bash
grep GPGGA /var/log/gps-logger/session_*/part_*.nmea | head
```

GGA の形式:

```text
$GPGGA,時刻,緯度,N,経度,E,fix品質,衛星数,HDOP,...
```

| 項目 | 例 | 意味 |
|---|---|---|
| 時刻 | `055012.00` | UTC 時刻 |
| 緯度 | `3439.1234,N` | 北緯 |
| 経度 | `13530.5678,E` | 東経 |
| fix 品質 | `1` | GPS fix あり |
| 衛星数 | `08` | 8 衛星使用 |
| HDOP | `1.2` | 精度指標 |

### 12. 1Hz / 9600bps の GPS モジュールを使う場合

このプロジェクトのデフォルトは 10Hz / 115200bps。工場出荷状態などで GPS モジュールが 1Hz / 9600bps の場合は、ロガー側も明示的に合わせる。

| 出力周期 | 推奨ボーレート |
|---|---|
| 1Hz | 9600bps |
| 5Hz | 38400bps 以上 |
| 10Hz | 115200bps |

GPS モジュール側の更新レート変更は [VK2828U7G5LF_GPSモジュール.md](VK2828U7G5LF_GPSモジュール.md) を参照。

Raspberry Pi 側を 1Hz / 9600bps に戻す場合は、systemd サービスの `ExecStart` を変更する。

```ini
ExecStart=/usr/bin/python3 /opt/gps-logger/gps_logger.py --port /dev/ttyAMA0 --baud 9600 --out /var/log/gps-logger --rotate-sec 300 --fsync-sec 2
```

反映する。

```bash
sudo systemctl daemon-reload
sudo systemctl restart gps-logger
sudo systemctl status gps-logger
```

### 13. トラブルシュート

#### ログが増えない

```bash
sudo systemctl status gps-logger
sudo journalctl -u gps-logger -n 100
```

| 原因 | 対策 |
|---|---|
| GPS TX/RX 配線ミス | GPS TX → Pi RX を確認 |
| UART 無効 | `raspi-config` で有効化 |
| `/dev/ttyAMA0` がない | UART 設定確認 |
| ボーレート違い | 9600 / 38400 / 115200 を試す |
| 電源不足 | 安定した電源を使う |

#### `GPRMC` がずっと `V`

| 原因 | 対策 |
|---|---|
| 屋内 | 屋外で測る |
| アンテナ向きが悪い | 空に向ける |
| 起動直後 | 10〜15分待つ |
| ビル街 | 開けた場所で試す |
| アンテナ不良 | アンテナ接続確認 |
| モジュール不良 | 別モジュールで確認 |

#### `$GPTXT,NMEA unknown msg` が出る

```text
$GPTXT,01,01,01,NMEA unknown msg*58
```

| 原因 | 対策 |
|---|---|
| GPS RX に何か送っている | GPS RX を未接続にする |
| gpsd がコマンド送信 | gpsd を停止する |
| ノイズ | 配線を短くする |
| TX/RX 接続が余計 | 受信だけの最小配線にする |

### 14. 初回測定の推奨手順

```text
1. 室内で NMEA ログが増えることを確認
2. GPS RX は未接続にする
3. gpsd を停止する
4. systemd 自動起動を有効化する
5. バッテリーを接続して起動する
6. 屋外の開けた場所に移動する
7. アンテナを空に向ける
8. 5〜10分静止する
9. その後、歩いて測定する
10. 帰宅後ログを回収する
11. GPRMC の A と GPGGA の衛星数を確認する
```

まずは **生 NMEA を外で確実に保存する**。解析や地図化は、ログ回収後に行う。

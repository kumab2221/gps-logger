# VK2828U7G5LF GPS モジュール配線ガイド

アンテナ付き VK2828U7G5LF GPS モジュールを Raspberry Pi 5 に接続するための配線メモ。

Raspberry Pi 側のロガー構築手順は [gps-logger-setup.md](gps-logger-setup.md) を参照。

## 基本方針

通常のログ取得では、GPS モジュールから Raspberry Pi に NMEA を送るだけでよい。

そのため、基本配線は以下の 3 本。

| GPS モジュール | Raspberry Pi 5 | 用途 |
|---|---|---|
| V | 3.3V | 電源 |
| G | GND | GND |
| T | GPIO15 / RXD0 / Pin 10 | GPS からの NMEA 受信 |

GPS モジュール側の `R` は、受信だけなら未接続にする。systemd 起動時に 10Hz / 115200bps へ自動復帰させる場合は、Raspberry Pi の `GPIO14 / TXD0 / Pin 8` に接続する。

```text
GPS T / TX → Raspberry Pi GPIO15 / RXD
GPS R / RX → 未接続、または自動復帰時は GPIO14 / TXD
```

GPS の設定を書き換える場合や `--prefer-10hz` を使う場合だけ、`R` を Raspberry Pi の `GPIO14 / TXD0 / Pin 8` に接続する。

## 1. ピンの意味

VK2828U7G5LF の標準ピンアサインは `E G R T V P` の 6 本。

| GPS モジュール | ケーブル色の例 | 意味 | ログ取得時の接続先 | ヘッダピン番号 |
|---|---|---|---|---|
| `V` | 赤 | VCC 電源入力 3.3V〜5V | 3.3V | Pin 1 |
| `G` | 黒 | GND | GND | Pin 6 |
| `T` | 青 | GPS の TX | GPIO15 / RXD0 | Pin 10 |
| `R` | 緑 | GPS の RX | 通常は未接続 | 設定時のみ Pin 8 |
| `E` | 黄 | Enable | 未接続で OK | - |
| `P` または `B` | 白 | PPS または VBAT | 通常は未接続 | PPS 使用時のみ Pin 7 など |

## 2. 配線

通常ログ取得時:

```text
VK2828U7G5LF                Raspberry Pi 5
                            40-pin GPIO header

V  赤  -------------------- Pin 1  (3.3V)
G  黒  -------------------- Pin 6  (GND)
T  青  -------------------- Pin 10 (GPIO15 / RXD)
R  緑  -------------------- 未接続
E  黄  -------------------- 未接続
P  白  -------------------- 未接続
```

設定変更時だけ:

```text
T  青  -------------------- Pin 10 (GPIO15 / RXD)
R  緑  -------------------- Pin 8  (GPIO14 / TXD)
```

TX/RX はクロス接続する。GPS の `T` は GPS 側の送信なので、Raspberry Pi 側の受信 `RXD` へ接続する。

## 3. GPS RX を普段つながない理由

GPS RX を Raspberry Pi の TX に接続すると、Raspberry Pi 側から何らかの文字列や制御データが GPS へ送られる可能性がある。

その場合、以下のようなメッセージが出ることがある。

```text
$GPTXT,01,01,01,NMEA unknown msg*58
```

受信ロガーとして使うだけなら、GPS RX は未接続の方が安全。

## 4. `P` または `B` ピンについて

標準シルク印刷は `E G R T V P` で、6 本目は `P`、つまり PPS(Pulse Per Second) が一般的。

| ピン | 用途 | 通常ログ取得 |
|---|---|---|
| `P` | 1 秒パルス出力 | 未接続で OK |
| `B` | VBAT の変種の可能性 | 未接続で OK |

PPS を時刻同期に使う場合は `Pin 7 / GPIO4` などに接続し、`gpsd` や `chrony` と組み合わせる。ただし、このロガーの基本目的は生 NMEA 保存なので PPS は必須ではない。

## 5. 電源

データシート上は 3.3V〜5V で動作するが、Raspberry Pi 5 と組み合わせる場合は 3.3V 給電を推奨。

- Raspberry Pi の GPIO は 3.3V 系
- 3.3V 給電にするとロジックレベルを揃えやすい
- 5V 給電では個体や基板構成によって TX 出力レベルに注意が必要

## 6. Raspberry Pi 5 側の UART

このプロジェクトでは Raspberry Pi 5 の GPIO14/15 UART を `/dev/ttyAMA0` として使う。

```bash
sudo raspi-config
```

```text
Interface Options
  → Serial Port
    → Login shell over serial? No
    → Serial hardware enabled? Yes
```

設定後に再起動する。

```bash
sudo reboot
ls -l /dev/ttyAMA0
```

## 7. 最小動作確認

`gpsd` が動いている場合は止める。

```bash
sudo systemctl stop gpsd gpsd.socket 2>/dev/null
sudo systemctl disable gpsd gpsd.socket 2>/dev/null
```

工場出荷状態のモジュールは 9600bps のことが多い。まず `9600` で確認する。10Hz / 115200bps へ設定済みなら `115200` で確認する。

```bash
stty -F /dev/ttyAMA0 9600
cat /dev/ttyAMA0
```

`$GPGGA` や `$GPRMC` で始まる文字列が流れてくれば配線は OK。

初回測位までは緯度経度が空、`GPRMC` が `V`、`GPGGA` の fix 品質が `0` になることがある。屋外または窓際で空が見える状態にする。

## 8. 更新レートを上げる場合

VK2828U7G5LF は最大 10Hz まで更新レートを上げられる。ただし、更新レートだけを上げるとシリアル帯域が不足する。

| 更新レート | 推奨ボーレート | 残す NMEA |
|---|---|---|
| 1Hz | 9600bps | 全部 OK |
| 5Hz | 38400bps 以上 | GGA, RMC, GSA |
| 10Hz | 115200bps | GGA, RMC 推奨 |

10Hz では、GGA/RMC/GSA/GSV/VTG/GLL をすべて出すと 9600bps では不足する。10Hz で使う場合は以下をセットで行う。

1. 更新レートを変更する
2. ボーレートを 115200bps などへ上げる
3. Dynamic Model を Automotive にする
4. SBAS/MSAS を有効化する
5. 不要な NMEA センテンスを無効化する
6. Raspberry Pi 側のロガー設定も同じボーレートにする

## 9. 10Hz / 115200bps に設定する

設定変更時は GPS `R` を Raspberry Pi `TXD` に接続する。

```text
GPS T / TX → Pi GPIO15 / RXD / Pin 10
GPS R / RX → Pi GPIO14 / TXD / Pin 8
GPS V      → 3.3V
GPS G      → GND
```

`python3-serial` がなければインストールする。

```bash
python3 -c "import serial" || sudo apt install python3-serial
```

設定用スクリプトを実行する。

```bash
sudo systemctl stop gps-logger
sudo systemctl stop gpsd gpsd.socket 2>/dev/null
sudo ./tools/configure_vk2828_10hz.py
```

このスクリプトは以下を行う。

| 設定 | 内容 |
|---|---|
| UART | 9600bps で接続後、115200bps へ変更 |
| 更新レート | 10Hz |
| Dynamic Model | Automotive |
| SBAS/MSAS | 有効、自動スキャン |
| NMEA | GGA/RMC を有効、GSA/GSV/VTG/GLL を無効 |
| 永続化 | Flash に保存 |

徒歩測定を主目的にする場合など、Dynamic Model を変更したい場合は `--dynamic-model` を指定する。

```bash
sudo ./tools/configure_vk2828_10hz.py --dynamic-model pedestrian
```

Dynamic Model を変更しない場合は `none` を指定する。

```bash
sudo ./tools/configure_vk2828_10hz.py --dynamic-model none
```

SBAS/MSAS を使わずに設定したい場合は `--no-sbas` を付ける。

```bash
sudo ./tools/configure_vk2828_10hz.py --no-sbas
```

設定後、115200bps で NMEA が流れることを確認する。

```bash
sudo stty -F /dev/ttyAMA0 115200
sudo timeout 10 cat /dev/ttyAMA0
```

## 10. u-center を使う方法

Windows 用の u-blox 公式 GUI ツール u-center でも同じ設定ができる。

| 画面 | 設定 |
|---|---|
| `View → Configuration View → RATE` | 更新レート |
| `View → Configuration View → PRT` | ボーレート |
| `View → Configuration View → MSG` | NMEA センテンスの有効/無効 |
| `CFG → Save current configuration` | Flash へ保存 |

一度 Flash に保存すれば、以降は Raspberry Pi 側から設定済みボーレートで読むだけでよい。

## 11. 注意点

- このプロジェクトの運用デフォルトは、起動時に 10Hz / 115200bps へ復帰を試す `--prefer-10hz`
- 起動時に 10Hz へ戻すには、GPS RX と Raspberry Pi TXD も接続する
- GPS RX が未接続の場合、ロガーは受信できるボーレートへフォールバックする
- 車載・バイク・自転車などの走行ログでは Dynamic Model は Automotive を推奨
- 徒歩中心の測定では `--dynamic-model pedestrian` も比較候補
- SBAS/MSAS は広島を含む日本国内で試す価値があるが、遮蔽やマルチパスを解消するものではない
- QZSS は GPS/QZSS L1 C/A として利用される。SLAS/CLAS のような高精度補強は、このモジュールでは期待しない
- PPS を使う場合、更新レート変更により PPS 周期にも影響することがあるため `UBX-CFG-TP5` の確認が必要
- アンテナ遮蔽がある場合、更新レートを上げても位置精度は改善しない
- 車載やモータースポーツ用途では 5Hz〜10Hz が目安

## 参考

- VK2828U7G5LF データシート: https://github.com/CainZ/V.KEL-GPS
- u-blox UBX-G7020-KT チップ採用
- u-blox 7 Receiver Description: https://www.u-blox.com/en/docs/UBX-13003221
- u-center: https://www.u-blox.com/en/product/u-center
- 工場出荷時のボーレート例: 9600bps
- デフォルト出力: GGA, GSA, GSV, RMC, VTG, GLL
- 最大更新レート: 10Hz

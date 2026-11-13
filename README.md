# Azza Indodax Dry-Run Trading Bot

Bot long-only untuk pasangan IDR di Indodax. Bot membaca candle tertutup dan order book publik, mensimulasikan fill dengan berjalan melewati level bid/ask, dan tidak mengirim order atau membutuhkan API key Indodax. Dashboard di `static/index.html` berfungsi sebagai halaman Vercel sekaligus file HTML mandiri: buka file itu langsung di browser, lalu impor JSON hasil ekspor untuk pemeriksaan offline.

## Status bahan UAS

Kode tidak membuat atau mengarang hasil. Data OHLCV asli Indodax untuk lebih dari tiga tahun kini tersimpan di `data/indodax_4h_history.json`, dan tarif resmi telah dibuktikan di `fee-evidence.json`. Satu snapshot nyata per pasangan dari 12 November belum memenuhi kebutuhan minimal enam snapshot pada sedikitnya dua hari, sehingga backtest berbiaya terukur belum dapat diterbitkan. Pada 14 November, satu pemanggilan dry-run manual dengan API publik mencatat sinyal BTCIDR SELL, ETHIDR SELL, dan XRPIDR HOLD ke state lokal `.bot_data.json`; log itu hanya pilot lokal, bukan forward test 14 hari. Ekspor lama `indodax-dry-run-log.json` tetap kosong. Deployment Vercel untuk commit `e57dfcb` selesai dibangun, tetapi domain production yang diberikan mengembalikan HTTP 404 pada `/`, `/api/health`, dan `/api/dashboard`. Konfigurasi routing telah diarahkan ke entrypoint FastAPI `main.py`; hasil deployment perbaikan masih perlu diuji. Run GitHub Actions tick terakhir gagal sebelum menghubungi aplikasi karena secret `CRON_SECRET` dan variable `VERCEL_DEPLOYMENT_URL` kosong; konfigurasi Upstash/Vercel dan `FORWARD_START_AT` produksi belum dapat diperiksa. Belum ada forward test 14 hari atau bukti freeze 5 November. Metrik tanpa bukti tetap ditampilkan sebagai belum tersedia.

## Strategi yang dikodekan

- Timeframe 4 jam; sumber candle dan waktu keputusan adalah API publik Indodax.
- Universe awal: BTC/IDR, ETH/IDR, XRP/IDR. Pastikan pilihan dan kriteria likuiditas terdokumentasi sebelum periode uji; jangan mengganti universe selama forward test.
- Masuk long saat penutupan candle melewati highest close 40 candle sebelumnya dan SMA 150; keluar saat penutupan di bawah lowest close 30 candle sebelumnya. Backtest dan bot memakai `crypto_bot/strategy.py`.
- Modal virtual Rp100.000.000; maksimum 3 posisi; maksimum 10% equity per posisi; daily loss limit 2% dan batas hari WIB; posisi terbuka dinilai pada best bid.
- Tidak ada short, margin, atau leverage. Spread di atas 2%, candle tidak valid/tidak lengkap, dan order book kosong membuat pasangan ditolak dan dicatat.
- Fee beli/jual berbeda sesuai tabel biaya IDR Indodax: beli taker all-in 0,20555% (service 0,20% + CFX 0,00555%, pajak 0%); jual taker all-in 0,41555% (service 0,20% + pajak 0,21% + CFX 0,00555%). Endpoint `/api/pairs` saat diverifikasi memberikan `trade_fee_percent_taker=0.2` untuk pasangan terpilih; kode menggunakan nilai lebih tinggi per sisi. Sumber resmi dan tanggal tercatat di `fee-evidence.json`.
- Minimum order dipatok sekurang-kurangnya Rp25.000; kuantitas dibulatkan menurut increment pasangan. Fill membeli berjalan di sisi ask dan fill menjual di sisi bid, termasuk level book berikutnya bila dibutuhkan.

Strategi dan parameter di atas adalah ketentuan yang sudah ada di kode; jangan mengubahnya saat forward test berjalan. Perubahan setelah freeze dicatat di [CHANGELOG.md](./CHANGELOG.md), termasuk penyesuaian sizing untuk menolak pengeluaran di atas kas; catat hash dan waktu commit sebagai perbaikan pasca-freeze, bukan bukti freeze.

## Menjalankan lokal

Gunakan Python 3.10+:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Isi `CRON_SECRET` dengan nilai acak lokal, lalu jalankan:

```powershell
uvicorn main:app --reload
```

Jalankan pengujian terfokus dengan dependensi pengembangan:

```powershell
py -m pip install -r requirements-dev.txt
py -m unittest discover -s tests -v
```

Workflow `.github/workflows/tests.yml` menjalankan suite yang sama pada Python 3.12 untuk setiap push dan pull request; runtime lokal/Vercel dipatok ke Python 3.12 di `.python-version`.

Buka `http://127.0.0.1:8000`. Endpoint:

Jangan buka `static/index.html` atau `Azza Dashboard Trading.backup.html` memakai preview HTML VS Code untuk data live; preview dapat memblokir endpoint API dengan 403. Jalankan server di atas dan buka `http://127.0.0.1:8000/`. File HTML tetap bisa dibuka langsung untuk mengimpor log secara offline.

- `GET /api/health` — mode, pasangan, dan timeframe.
- `GET /api/dashboard` — state dan log forward test.
- `POST /api/tick` — satu pemanggilan bot; wajib `Authorization: Bearer <CRON_SECRET>`.
- `POST /api/snapshot` — snapshot order book mentah dan estimasi slippage; wajib secret yang sama.
- `POST /api/kill-switch` — body `{"enabled":true}` atau `{"enabled":false}`, dilindungi secret yang sama.

Penyimpanan lokal menggunakan `.bot_data.json`. File ini diabaikan Git. Data lokal bukan storage persisten untuk Vercel.

## Deploy ke Vercel dan penjadwal 4 jam

Vercel memakai `main.py` sebagai entrypoint FastAPI dan `vercel.json` mempertahankan batas durasi function 300 detik tanpa rewrite path. Vercel menggunakan runtime Python 3.12 yang dipatok di `.python-version`. Pasang environment variables berikut di Vercel:

- `CRON_SECRET` — secret kuat dan sama dengan GitHub Actions secret.
- `UPSTASH_REDIS_REST_URL` dan `UPSTASH_REDIS_REST_TOKEN` — Upstash Redis REST untuk state, lock, dan log persisten.
- `INDODAX_UNIVERSE=BTCIDR,ETHIDR,XRPIDR`.
- `INDODAX_ALL_IN_TAKER_BUY_FEE_RATE` dan `INDODAX_ALL_IN_TAKER_SELL_FEE_RATE` — tarif taker all-in desimal per sisi, paling tidak sebesar tarif `/api/pairs`.
- `FORWARD_START_AT` — isi waktu UTC saat deployment dan scheduler benar-benar mulai berjalan. Jangan gunakan tanggal freeze yang telah lewat; log periode lama tidak boleh direkayasa.

Paket Hobby Vercel hanya menjadwalkan sekali sehari, sehingga workflow `.github/workflows/tick.yml` memanggil `/api/tick` setiap empat jam. Di pengaturan repository GitHub buat secret `CRON_SECRET` serta repository variable `VERCEL_DEPLOYMENT_URL` berisi URL deployment HTTPS. Workflow mendukung `workflow_dispatch` untuk uji manual. Pastikan `/api/health`, `/api/dashboard`, dan pemanggilan terotorisasi `/api/tick` bekerja sebelum forward test.

Jangan commit `.env`, token Upstash, secret scheduler, atau file state. `.env.example` hanya berisi nama konfigurasi tanpa kredensial.

## Log, dashboard, dan hitung ulang

Log setiap candle yang diproses memuat sinyal, alasan, waktu, bid/ask, dan snapshot penuh order book; catatan order memuat kuantitas, harga fill rata-rata, fee, serta selisih terhadap harga sinyal. Panel “Log sinyal aktual” menampilkan semua event sinyal tersimpan (maksimum 10.000 event sesuai batas storage), termasuk waktu penutupan candle, harga, Donchian entry/exit, dan SMA. Panel tersebut tidak disaring berdasarkan usia forward test; periode 14 hari hanya syarat evaluasi final, bukan syarat mencatat atau melihat sinyal. Dashboard juga membedakan tidak adanya log dari sinyal HOLD dan menampilkan waktu pembaruan API beserta waktu candle terakhir. Return dan drawdown forward baru ditampilkan setelah `FORWARD_START_AT` disetel; durasi yang kurang dari 14 hari tetap pilot, bukan hasil final. Panggilan terjadwal, penolakan data, error Indodax, perubahan kill switch, dan equity harian ikut dicatat. Workflow `.github/workflows/snapshot.yml` mengambil snapshot order book mentah tiga kali sehari melalui endpoint terlindungi `/api/snapshot`, untuk ukuran order Rp10.000.000. Setelah mendapat minimal enam sampel pada sedikitnya dua hari per pasangan, ekspor JSON dashboard, simpan snapshot ke `data/orderbook_snapshots.json`, dan jalankan backtest:

```powershell
py collect_indodax_history.py --start 2023-11-12T00:00:00Z --output data/indodax_4h_history.json
py run_indodax_backtest.py --data data/indodax_4h_history.json --snapshots data/orderbook_snapshots.json
```

Downloader membatasi permintaan berurutan sampai maksimum 2,5 request/detik dan menyimpan candle OHLCV asli dari API publik. Universe BTC/ETH/XRP ditetapkan sebelum pengunduhan; likuiditas dinilai dari 30 hari pertama periode pada set kandidat tetap. Kandidat tersebut tidak mencakup pasangan yang sudah delisting sebelum koleksi, sehingga keterbatasan survivorship tetap dinyatakan di hasil. Median spread dan slippage order book diukur dari snapshot nyata saat ini lalu diterapkan sebagai asumsi biaya untuk candle historis; bukan snapshot historis dan wajib dijelaskan sebagai keterbatasan. Hasil hanya dibuat jika syarat sampel snapshot terpenuhi.

Forward test 14 hari harus dimulai dari deployment baru dan tidak dapat dinyatakan selesai sebelum 14 hari benar-benar berlalu. Pilot singkat hanya menjadi catatan operasional sementara, bukan pengganti periode tersebut; metrik yang belum dapat dihitung ditampilkan sebagai belum tersedia, bukan return 0. Ekspor JSON dashboard dapat diimpor kembali ke HTML yang dibuka offline. Untuk menghitung ulang metrik dari file ekspor:

```powershell
py hitung_ulang.py indodax-dry-run-log.json --output hitung-ulang.json
```

Simpan hasil JSON hitung ulang bersama file ekspor sebagai bukti audit; timestamp ekspor dipakai agar hitungan uptime dapat diulang.

Metrik hanya dapat dinilai setelah log nyata tersedia. Backtest wajib memakai candle historis Indodax minimal tiga tahun dan biaya/spread/slippage yang benar-benar diukur dari snapshot; backtest selama jendela 14 hari yang sama juga harus menunjukkan kecocokan sinyal candle demi candle. Benchmark adalah buy-and-hold BTC/IDR pada jendela yang sama. Jangan mengisi `crypto_bot/backtest_result.json` dengan angka manual atau data simulasi.

## Refleksi (status)

Refleksi kuantitatif belum dapat disimpulkan sebelum forward log lengkap dan backtest jendela yang sama tersedia. Saat tersedia, bandingkan sinyal per candle terlebih dahulu; lalu jelaskan setiap selisih fill memakai fee, spread, slippage terhadap harga sinyal, serta waktu eksekusi setelah candle tutup. Hasil tanpa log atau dengan cakupan sinyal parsial harus dilaporkan sebagai belum lengkap, bukan sebagai kecocokan penuh.

## Berkas utama

- `crypto_bot/strategy.py` — aturan sinyal yang dipakai bot.
- `crypto_bot/exchange.py` — API publik Indodax.
- `crypto_bot/execution.py` — simulasi fill order book.
- `crypto_bot/bot.py` — risk management, idempotency, dan tick.
- `crypto_bot/storage.py` — storage lokal dan Upstash.
- `main.py` — API FastAPI, dashboard, endpoint tick, dan kill switch.
- `static/index.html` — dashboard live dan HTML offline mandiri.
- `hitung_ulang.py` — hitung ulang metrik forward/operasional dari ekspor JSON.
- `collect_indodax_history.py` — unduh OHLCV 4 jam asli Indodax dengan rate limit.
- `run_indodax_backtest.py` — jalankan strategi bersama dengan biaya median dari snapshot yang diukur; opsi `--forward-log indodax-dry-run-log.json` membandingkan sinyal per candle serta fill per order pada jendela forward yang sama.
- `fee-evidence.json` — tarif taker resmi per sisi dan perbandingan API publik, diakses 12 November 2026.
- `CHANGELOG.md` — catatan perbaikan pasca-freeze untuk audit forward test.
- `main.py`, `vercel.json`, `.github/workflows/` — API Vercel, scheduler tick, pengambilan snapshot, dan tes CI.
Arsip dashboard/backtest Kompas100 lama yang ada di folder kerja bukan bagian dari aplikasi Indodax ini dan tidak disertakan dalam repository.

# Catatan perubahan forward test

## 2026-11-09 — perbaikan operasional setelah tanggal freeze

Dicatat sebagai perbaikan bug/instrumentasi, bukan sebagai hasil uji atau perubahan sinyal:

- Menambahkan snapshot penuh bid/ask ke log dan memperjelas pencatatan error API, waktu jadwal, equity harian, dan mark posisi yang stale.
- Menambahkan endpoint dan scheduler snapshot order book tiga kali sehari.
- Menggunakan fee taker all-in yang berbeda untuk buy dan sell berdasarkan tabel resmi IDR (0,20555% dan 0,41555%).
- Memperbaiki sizing agar order simulasi diturunkan sampai total fill dan fee tidak melebihi kas yang tersedia.
- Memvalidasi metadata minimum order, fee, kuantitas, dan kenaikan harga sebelum menghitung sinyal pasangan.
- Memuat `.env` untuk konfigurasi lokal dan mendokumentasikan bukti tarif resmi.
- Menambahkan entrypoint Vercel, workflow scheduler empat jam, dashboard offline/live, dan skrip hitung ulang.

Parameter sinyal, timeframe, universe default, batas risiko, dan aturan entry/exit tidak diubah. Penyesuaian sizing hanya berlaku bila estimasi fill melewati kas yang tersedia. Catatan ini tidak membuktikan commit freeze 5 November atau forward test telah berjalan. Simpan hash dan waktu commit perubahan ini, dan jangan menyatakan perubahan tersebut sudah ada sebelum freeze.

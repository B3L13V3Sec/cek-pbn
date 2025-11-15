#!/usr/bin/env python3
import asyncio
import csv
import sys
from collections import Counter

import httpx

# --- Konfigurasi dasar ---
TIMEOUT_SECONDS = 7          # timeout per request
MAX_CONCURRENCY = 20         # jumlah domain yang dicek paralel
OUTPUT_FILE = "hasil-pbn.csv"

PARKING_KEYWORDS = [
    "buy this domain", "this domain is for sale",
    "domain is parked", "parkingcrew", "sedo",
    "afternic", "dan.com", "expired domain",
    "has expired", "renew it now"
]


def build_candidate_urls(raw: str):
    """
    Membangun list URL kandidat yang akan dicek.
    Logika:
    - Kalau user tidak tulis http/https -> coba https dulu, lalu http (fallback).
    - Kalau user tulis https://domain -> coba https dulu, lalu http (fallback).
    - Kalau user tulis http://domain -> hanya http (tidak fallback ke https).
    """
    raw = raw.strip()
    lower = raw.lower()

    if not raw:
        return []

    if lower.startswith("http://"):
        return [raw]  # hanya http

    if lower.startswith("https://"):
        # https dulu, kalau gagal baru http
        tanpa_scheme = raw.split("://", 1)[1]
        return [raw, f"http://{tanpa_scheme}"]

    # Kalau tidak ada scheme -> https dulu, lalu http
    return [f"https://{raw}", f"http://{raw}"]


def is_wordpress(html_text: str) -> bool:
    """Deteksi sederhana apakah halaman ini WordPress."""
    if not html_text:
        return False

    lower = html_text.lower()
    markers = [
        "wp-content",
        "wp-includes",
        "wp-json",
        'content="wordpress',
        "generator\" content=\"wordpress"
    ]
    return any(m in lower for m in markers)


def is_parking_page(html_text: str) -> bool:
    """Deteksi kasar apakah halaman ini parked/for sale."""
    if not html_text:
        return False

    lower = html_text.lower()
    return any(k in lower for k in PARKING_KEYWORDS)


def classify_transport_error(exc: Exception):
    """
    Klasifikasi error kalau bahkan tidak dapat HTTP response sama sekali.
    Contoh: DNS error, timeout, SSL error, connection refused.
    """
    msg = str(exc).lower()

    status = "ERROR_TIDAK_BISA_DIBUKA"
    notes = msg

    # Sedikit heuristik buat "mungkin expired/parked"
    if "name or service not known" in msg or "nxdomain" in msg or "not known" in msg:
        status = "PARKED_ATAU_MUNGKIN_EXPIRED"
        notes = "dns_error: " + msg
    elif "timed out" in msg or "timeout" in msg:
        status = "ERROR_TIDAK_BISA_DIBUKA"
        notes = "timeout: " + msg
    elif "ssl" in msg or "certificate" in msg:
        status = "ERROR_TIDAK_BISA_DIBUKA"
        notes = "ssl_error: " + msg

    return status, None, None, notes


def classify_response(resp, used_scheme: str):
    """
    Klasifikasi berdasarkan HTTP response.
    Mengembalikan: status, http_status, final_url, notes
    """
    http_status = resp.status_code
    final_url = str(resp.url)
    notes = ""

    text_snippet = ""
    try:
        text_snippet = resp.text[:4000]  # batasi, biar nggak kegedean
    except Exception:
        text_snippet = ""

    # Kalau status 200 (OK)
    if 200 <= http_status < 300:
        if is_wordpress(text_snippet):
            return "AKTIF_WORDPRESS", http_status, final_url, f"wp_markers_found ({used_scheme})"

        if is_parking_page(text_snippet):
            return "PARKED_ATAU_MUNGKIN_EXPIRED", http_status, final_url, f"parking_keywords_found ({used_scheme})"

        return "AKTIF_NON_WORDPRESS", http_status, final_url, f"no_wp_markers ({used_scheme})"

    # Kalau 3xx biasanya sudah di-follow sama httpx (follow_redirects=True),
    # tapi kalau masih 3xx di sini, anggap saja 'aktif tapi aneh'
    if 300 <= http_status < 400:
        return "AKTIF_NON_WORDPRESS", http_status, final_url, f"3xx_status ({used_scheme})"

    # 4xx / 5xx -> servernya ada, tapi bermasalah. Kita anggap error.
    if 400 <= http_status < 600:
        if is_parking_page(text_snippet):
            return "PARKED_ATAU_MUNGKIN_EXPIRED", http_status, final_url, f"http_{http_status}_parking ({used_scheme})"
        return "ERROR_TIDAK_BISA_DIBUKA", http_status, final_url, f"http_{http_status} ({used_scheme})"

    # fallback
    return "ERROR_TIDAK_BISA_DIBUKA", http_status, final_url, f"unexpected_status ({used_scheme})"


async def check_one_domain(client, domain: str):
    """
    Cek satu domain dengan logika:
    - Bangun daftar URL kandidat (https dulu, lalu http)
    - Loop ke setiap URL:
      - Kalau request berhasil -> klasifikasikan dan langsung return
      - Kalau request error -> simpan exception, lanjut ke kandidat berikutnya
    - Kalau semua kandidat gagal -> klasifikasikan berdasarkan exception terakhir
    """
    domain = domain.strip()
    if not domain or domain.startswith("#"):
        return None

    urls = build_candidate_urls(domain)
    last_exc = None

    for url in urls:
        used_scheme = "https" if url.lower().startswith("https://") else "http"
        try:
            resp = await client.get(
                url,
                follow_redirects=True,
                timeout=TIMEOUT_SECONDS,
                headers={
                    "User-Agent": "Mozilla/5.0 (PBN-Checker)"
                },
            )
            status, http_status, final_url, notes = classify_response(resp, used_scheme)
            return {
                "domain": domain,
                "status": status,
                "http_status": http_status,
                "final_url": final_url,
                "notes": notes,
            }
        except Exception as exc:
            last_exc = exc
            continue

    # Kalau semua kandidat gagal (https dan http)
    status, http_status, final_url, notes = classify_transport_error(last_exc)
    return {
        "domain": domain,
        "status": status,
        "http_status": http_status,
        "final_url": final_url,
        "notes": notes,
    }


async def run(domains_file: str):
    # Baca list domain
    with open(domains_file, "r", encoding="utf-8") as f:
        domains = [line.strip() for line in f if line.strip()]

    print(f"Total domain yang akan dicek: {len(domains)}")

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    results = []

    async with httpx.AsyncClient(verify=True) as client:
        async def worker(d):
            async with sem:
                return await check_one_domain(client, d)

        tasks = [worker(d) for d in domains]
        for coro in asyncio.as_completed(tasks):
            res = await coro
            if res:
                results.append(res)
                print(f"[{res['status']}] {res['domain']} -> {res['final_url'] or '-'}")

    # Tulis ke CSV
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        fieldnames = ["domain", "status", "http_status", "final_url", "notes"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(r)

    # Ringkasan
    counter = Counter(r["status"] for r in results)
    print("\n=== RINGKASAN ===")
    for status, count in counter.items():
        print(f"{status:25s}: {count}")
    print(f"\nDetail tersimpan di file: {OUTPUT_FILE}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Pemakaian: python cek_pbn_wp.py domains.txt")
        sys.exit(1)

    domains_file = sys.argv[1]
    asyncio.run(run(domains_file))

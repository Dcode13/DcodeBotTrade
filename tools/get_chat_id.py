"""Bantu temukan OWNER_CHAT_ID Telegram.

Cara pakai:
1. Isi TELEGRAM_BOT_TOKEN di .env dulu.
2. Buka Telegram, cari bot kamu, tekan START, kirim pesan apa saja (mis. "halo").
3. Jalankan:  python tools\\get_chat_id.py
4. Salin angka "chat_id" yang muncul ke OWNER_CHAT_ID di .env.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Konsol Windows default cp1252 -> paksa UTF-8 agar emoji tidak crash.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


def main() -> None:
    load_dotenv(ROOT / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN belum diisi di .env.")
        sys.exit(1)

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        resp = requests.get(url, params={"timeout": 0}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"❌ Gagal menghubungi Telegram: {exc}")
        print("   Cek token benar & koneksi internet.")
        sys.exit(1)

    if not data.get("ok"):
        print(f"❌ Telegram menolak token: {data}")
        sys.exit(1)

    results = data.get("result", [])
    if not results:
        print("⚠️  Belum ada pesan masuk.")
        print("    Buka bot kamu di Telegram, tekan START, kirim pesan apa saja,")
        print("    lalu jalankan skrip ini lagi.")
        return

    seen: dict[str, str] = {}
    for upd in results:
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat", {})
        cid = chat.get("id")
        if cid is None:
            continue
        name = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
        seen[str(cid)] = name

    if not seen:
        print("⚠️  Ada update tapi tanpa chat. Kirim pesan teks biasa ke bot.")
        return

    print("✅ Chat ID ditemukan:")
    for cid, name in seen.items():
        print(f"   OWNER_CHAT_ID={cid}   (dari: {name})")
    print("\nSalin salah satu OWNER_CHAT_ID di atas ke file .env.")


if __name__ == "__main__":
    main()

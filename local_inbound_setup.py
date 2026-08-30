"""首次配置本地数据库入站，不记录任何明文密钥。"""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path

from local_inbound_credentials import (
    CredentialError,
    load_credentials,
    save_credentials,
)

DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "local_inbound_credentials.json"


def _interactive_values() -> dict:
    account_dir = input("微信账号目录（包含 db_storage）：").strip()
    wxid = input("微信真 ID（wxid）：").strip()
    return {
        "account_dir": account_dir,
        "wxid": wxid,
        "db_key": getpass.getpass("64 位十六进制数据库密钥：").strip(),
        "image_xor_key": getpass.getpass("图片 XOR 密钥（可暂时留空）：").strip(),
        "image_aes_key": getpass.getpass("图片 AES 密钥（可暂时留空）：").strip(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="配置 WeMai 本地微信数据库入站")
    parser.add_argument("--output", default=str(DEFAULT_PATH), help="DPAPI 凭据文件")
    parser.add_argument("--status", action="store_true", help="仅验证当前凭据")
    args = parser.parse_args(argv)
    try:
        if args.status:
            values = load_credentials(args.output)
            print(
                "配置有效："
                f"wxid={values['wxid']} account_dir={values['account_dir']} "
                f"image_keys={'yes' if values['image_xor_key'] and values['image_aes_key'] else 'no'}"
            )
            return 0
        values = _interactive_values()
        save_credentials(args.output, **values)
        loaded = load_credentials(args.output)
        print(f"配置已写入并通过 DPAPI 回读验证：{Path(args.output).resolve()}")
        print(f"账号：{loaded['wxid']}，数据库目录：{loaded['account_dir']}")
        return 0
    except (CredentialError, OSError) as exc:
        print(f"配置失败：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

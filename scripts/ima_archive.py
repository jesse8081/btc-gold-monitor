#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把报表仓库 reports/ 下的 markdown 日报归档进 IMA 知识库。

设计目标：跑在 GitHub Actions 上，完全不依赖本地电脑。
只用标准库（urllib + hmac），CI 里不需要 pip install。

用法：
  python scripts/ima_archive.py                    # 归档北京时间今天的报告（无则跳过）
  python scripts/ima_archive.py --date 2026-10-22  # 指定日期
  python scripts/ima_archive.py --backfill 5       # 回补最近 5 天里缺的报告

环境变量：
  IMA_CLIENT_ID   IMA 开放平台 Client ID（必须）
  IMA_API_KEY     IMA 开放平台 API Key（必须）
  IMA_KB_NAME     目标知识库名称，默认「投资」
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

IMA_BASE = "https://ima.qq.com/openapi/wiki/v1"
BJ_TZ = timezone(timedelta(hours=8))

MEDIA_TYPE_MD = 7
CONTENT_TYPE_MD = "text/markdown"
MAX_MD_BYTES = 10 * 1024 * 1024  # IMA 对 Markdown 的大小上限

# 报告正文必须能通过这个校验，避免把半截/错误页面塞进知识库
REQUIRED_HEAD = "# 股指期货贴水跟踪日报"
REQUIRED_SECTION = "## 一、主力合约贴水快照"

RETRYABLE_CODES = {"110010", "110021"}  # 下游网络错误 / 请求频控


def log(msg):
    print(msg, flush=True)


def _creds():
    cid = (os.environ.get("IMA_CLIENT_ID") or "").strip()
    key = (os.environ.get("IMA_API_KEY") or "").strip()
    if not cid or not key:
        raise SystemExit(
            "缺少 IMA 凭证：请在仓库 Settings → Secrets and variables → Actions "
            "里添加 IMA_CLIENT_ID 和 IMA_API_KEY"
        )
    return cid, key


def ima_api(path, body, retries=4):
    """调用 IMA OpenAPI，返回 data 字段；retcode != 0 抛异常。"""
    cid, key = _creds()
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "ima-openapi-clientid": cid,
        "ima-openapi-apikey": key,
    }
    last_err = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(IMA_BASE + path, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code}"
            if exc.code >= 500 and attempt < retries:
                time.sleep(3 * attempt)
                continue
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise RuntimeError(f"{path} {last_err} {detail}") from None
        except Exception as exc:  # 网络抖动
            last_err = str(exc)
            if attempt < retries:
                time.sleep(3 * attempt)
                continue
            raise RuntimeError(f"{path} 网络错误: {last_err}") from None

        try:
            res = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(f"{path} 返回非 JSON: {raw[:200]}") from None

        code = res.get("retcode", res.get("code", -1))
        if str(code) == "0":
            return res.get("data") or {}
        errmsg = res.get("errmsg") or res.get("msg") or ""
        if str(code) in RETRYABLE_CODES and attempt < retries:
            time.sleep(3 * attempt)
            continue
        raise RuntimeError(f"{path} 失败 retcode={code} errmsg={errmsg}")

    raise RuntimeError(f"{path} 多次重试仍失败: {last_err}")


def resolve_kb_id(name):
    """按名称取知识库 ID（不硬编码，避免 IMA 迁移 ID 后失效）。"""
    cursor, seen = "", 0
    while True:
        data = ima_api("get_addable_knowledge_base_list", {"cursor": cursor, "limit": 50})
        for kb in data.get("addable_knowledge_base_list") or []:
            if (kb.get("name") or "").strip() == name:
                return kb["id"]
        seen += 1
        if data.get("is_end") or seen > 5:
            break
        cursor = data.get("next_cursor") or ""
        if not cursor:
            break
    raise RuntimeError(f"没找到名为「{name}」的可写知识库，请确认名称或账号权限")


def existing_titles(kb_id, limit_pages=4):
    """拉取知识库根目录已有条目标题，用于去重。"""
    titles, cursor = set(), ""
    for _ in range(limit_pages):
        data = ima_api(
            "get_knowledge_list",
            {"knowledge_base_id": kb_id, "cursor": cursor, "limit": 50},
        )
        for item in data.get("knowledge_list") or []:
            title = item.get("title") or (item.get("folder_info") or {}).get("name")
            if title:
                titles.add(title)
        if data.get("is_end"):
            break
        cursor = data.get("next_cursor") or ""
        if not cursor:
            break
    return titles


def _hmac_sha1_hex(key, msg):
    return hmac.new(key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha1).hexdigest()


def _sha1_hex(msg):
    return hashlib.sha1(msg.encode("utf-8")).hexdigest()


def build_authorization(secret_id, secret_key, pathname, content_length, host, start_time, expired_time):
    """生成 COS PUT Object 的 Authorization 头（签名算法与 IMA 官方脚本一致）。"""
    key_time = f"{start_time};{expired_time}"
    sign_headers = {"content-length": str(content_length), "host": host}
    header_keys = sorted(sign_headers)
    http_headers = "&".join(
        f"{k}={urllib.parse.quote(str(sign_headers[k]), safe='')}" for k in header_keys
    )
    http_string = f"put\n{pathname}\n\n{http_headers}\n"

    sign_key = _hmac_sha1_hex(secret_key, key_time)
    string_to_sign = f"sha1\n{key_time}\n{_sha1_hex(http_string)}\n"
    signature = _hmac_sha1_hex(sign_key, string_to_sign)
    return "&".join(
        [
            "q-sign-algorithm=sha1",
            f"q-ak={secret_id}",
            f"q-sign-time={key_time}",
            f"q-key-time={key_time}",
            f"q-header-list={';'.join(header_keys)}",
            "q-url-param-list=",
            f"q-signature={signature}",
        ]
    )


def cos_put(cred, body, content_type):
    """按 COS 签名规范 PUT 对象；签名算法与 IMA 官方 cos-upload 脚本一致。"""
    secret_id = cred["secret_id"]
    secret_key = cred["secret_key"]
    token = cred.get("token") or ""
    bucket = cred["bucket_name"]
    region = cred["region"]
    cos_key = cred["cos_key"]

    now = int(time.time())

    def _ts(v, default):
        try:
            v = int(float(v))
        except (TypeError, ValueError):
            return default
        return v // 1000 if v > 10**12 else v

    start_time = _ts(cred.get("start_time"), now)
    expired_time = _ts(cred.get("expired_time"), now + 3600)

    host = f"{bucket}.cos.{region}.myqcloud.com"
    pathname = "/" + cos_key.lstrip("/")
    authorization = build_authorization(
        secret_id, secret_key, pathname, len(body), host, start_time, expired_time
    )

    url = f"https://{host}{urllib.parse.quote(pathname, safe='/-_.~')}"
    req = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Authorization": authorization,
            "x-cos-security-token": token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            if not (200 <= resp.status < 300):
                raise RuntimeError(f"COS 上传返回 HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"COS 上传失败 HTTP {exc.code} {detail}") from None


def archive_one(kb_id, date_str, text, titles, dry_run=False):
    """归档单篇；返回 'added' / 'skipped'。"""
    file_name = f"股指期货贴水日报_{date_str}.md"
    body = text.encode("utf-8")

    if file_name in titles:
        log(f"  · {file_name} 已在库中，跳过")
        return "skipped"

    if len(body) > MAX_MD_BYTES:
        raise RuntimeError(f"{file_name} 大小 {len(body)} 字节，超过 Markdown 10MB 上限")

    if dry_run:
        log(f"  · [dry-run] 待上传 {file_name}（{len(body)} 字节）")
        return "added"

    dup = ima_api(
        "check_repeated_names",
        {
            "knowledge_base_id": kb_id,
            "params": [{"name": file_name, "media_type": MEDIA_TYPE_MD}],
        },
    )
    for item in dup.get("results") or []:
        if item.get("name") == file_name and item.get("is_repeated"):
            log(f"  · {file_name} 库中已存在同名文件，跳过")
            return "skipped"

    media = ima_api(
        "create_media",
        {
            "file_name": file_name,
            "file_size": len(body),
            "content_type": CONTENT_TYPE_MD,
            "knowledge_base_id": kb_id,
            "file_ext": "md",
        },
    )
    media_id = media.get("media_id")
    cred = media.get("cos_credential") or {}
    if not media_id or not cred.get("cos_key"):
        raise RuntimeError(f"create_media 返回异常: {json.dumps(media, ensure_ascii=False)[:300]}")

    cos_put(cred, body, CONTENT_TYPE_MD)

    ima_api(
        "add_knowledge",
        {
            "media_type": MEDIA_TYPE_MD,
            "media_id": media_id,
            "title": file_name,
            "knowledge_base_id": kb_id,
            "file_info": {
                "cos_key": cred["cos_key"],
                "file_size": len(body),
                "file_name": file_name,
            },
        },
    )
    titles.add(file_name)
    log(f"  · {file_name} 已归档（{len(body)} 字节）")
    return "added"


def load_report(reports_dir, date_str):
    path = os.path.join(reports_dir, f"{date_str}.md")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if REQUIRED_HEAD not in text or REQUIRED_SECTION not in text:
        log(f"  · {date_str} 报告格式不符合预期，跳过（避免写入残缺内容）")
        return None
    return text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="只归档指定日期 YYYY-MM-DD")
    parser.add_argument("--backfill", type=int, default=0, help="回补最近 N 天（自然日）里缺的报告")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--kb-name", default=os.environ.get("IMA_KB_NAME", "投资"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    today = datetime.now(BJ_TZ).date()
    if args.date:
        try:
            targets = [datetime.strptime(args.date, "%Y-%m-%d").date()]
        except ValueError:
            raise SystemExit("--date 需要 YYYY-MM-DD 格式")
    elif args.backfill > 0:
        targets = [today - timedelta(days=i) for i in range(args.backfill - 1, -1, -1)]
    else:
        targets = [today]

    log(f"目标日期: {', '.join(d.isoformat() for d in targets)}")

    reports = []
    for day in targets:
        text = load_report(args.reports_dir, day.isoformat())
        if text is None:
            log(f"  · {day} 无可用报告（非交易日或尚未生成），跳过")
            continue
        reports.append((day.isoformat(), text))

    if not reports:
        log("没有需要归档的报告，结束。")
        return 0

    if args.dry_run:
        kb_id, titles = "(dry-run)", set()
    else:
        kb_id = resolve_kb_id(args.kb_name)
        log(f"目标知识库「{args.kb_name}」")
        titles = existing_titles(kb_id)

    added = skipped = 0
    failed = []
    for date_str, text in reports:
        try:
            result = archive_one(kb_id, date_str, text, titles, dry_run=args.dry_run)
            added += result == "added"
            skipped += result == "skipped"
        except Exception as exc:
            failed.append((date_str, str(exc)))
            log(f"  ! {date_str} 归档失败: {exc}")

    log("")
    log(f"完成：新增 {added} 篇，跳过 {skipped} 篇，失败 {len(failed)} 篇")
    if failed:
        for date_str, err in failed:
            log(f"  失败 {date_str}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

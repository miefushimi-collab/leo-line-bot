#!/usr/bin/env python3
"""
LINE × Claude × freee 経理アシスタントBot
- トークンは /data/freee_tokens.json に永続保存（Railway Volume）
- リフレッシュのたびに最新トークンをファイルに書き込む
"""

import os
import json
import time
import logging
import threading
import requests
import anthropic
from pathlib import Path
from datetime import date
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 環境変数
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
FREEE_CLIENT_ID = os.environ["FREEE_CLIENT_ID"]
FREEE_CLIENT_SECRET = os.environ["FREEE_CLIENT_SECRET"]
FREEE_COMPANY_ID = int(os.environ["FREEE_COMPANY_ID"])

# 初期リフレッシュトークン（Railway Volume がない初回用のフォールバック）
FREEE_REFRESH_TOKEN_INITIAL = os.environ.get("FREEE_REFRESH_TOKEN", "")

FREEE_TOKEN_URL = "https://accounts.secure.freee.co.jp/public_api/token"
FREEE_API_BASE = "https://api.freee.co.jp/api/1"

# Railway Volume のトークン保存先
TOKEN_FILE = Path("/data/freee_tokens.json")

handler = WebhookHandler(LINE_CHANNEL_SECRET)
line_configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_token_lock = threading.Lock()


# ── トークンファイル読み書き ──────────────────────────────────

def load_token_cache() -> dict:
    """Volume ファイル → env var の順でトークンを初期化する"""
    if TOKEN_FILE.exists():
        try:
            data = json.loads(TOKEN_FILE.read_text())
            logger.info("トークンをファイルから読み込みました")
            return {
                "access_token": data.get("access_token"),
                "refresh_token": data.get("refresh_token", FREEE_REFRESH_TOKEN_INITIAL),
                "expires_at": float(data.get("expires_at", 0)),
            }
        except Exception as e:
            logger.warning("トークンファイル読み込み失敗: %s", e)

    logger.info("トークンファイルなし。env var から初期化します")
    return {
        "access_token": None,
        "refresh_token": FREEE_REFRESH_TOKEN_INITIAL,
        "expires_at": 0,
    }


def save_token_cache(cache: dict) -> None:
    """トークンを Volume ファイルに保存する"""
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
        logger.info("トークンをファイルに保存しました")
    except Exception as e:
        logger.warning("トークンファイル保存失敗: %s", e)


_token_cache = load_token_cache()

SYSTEM_PROMPT = """あなたはLeo経理アシスタントです。freeeの会計データの照会・取引登録をサポートします。
今日の日付は {today} です。

【照会】
取引・売上・支出・経費について聞かれたら get_deals を使ってfreeeからデータを取得してください。

【取引登録】
売上や支出の登録を依頼されたら、以下の手順で進めてください。
1. 不足情報があれば確認する（取引先名、金額、日付、種別）
2. get_account_items で勘定科目一覧を取得して適切な科目を選ぶ
3. create_deal で登録する
4. 登録完了を報告する

日付が「今日」「昨日」などの場合は今日の日付から計算してください。
金額は数字のみで扱い（カンマ・円マーク不要）、勘定科目はユーザーの言葉から最適なものを選んでください。
回答はLINEのチャット形式で読みやすく、簡潔にまとめてください。"""

TOOLS = [
    {
        "name": "get_deals",
        "description": "freeeから取引一覧を取得します。売上・支出・経費などの照会に使います。",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "取得開始日（YYYY-MM-DD）。省略時は今月1日。"},
                "end_date": {"type": "string", "description": "取得終了日（YYYY-MM-DD）。省略時は今日。"},
                "deal_type": {"type": "string", "enum": ["income", "expense"], "description": "income=収入のみ、expense=支出のみ。省略時は両方。"},
            },
            "required": [],
        },
    },
    {
        "name": "get_account_items",
        "description": "freeeの勘定科目一覧を取得します。取引登録前に適切な勘定科目IDを調べるために使います。",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "絞り込みキーワード（例：売上、仕入、交通）。省略時は全件。"},
            },
            "required": [],
        },
    },
    {
        "name": "create_deal",
        "description": "freeeに取引（売上・支出）を登録します。",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_date": {"type": "string", "description": "取引日（YYYY-MM-DD）。"},
                "deal_type": {"type": "string", "enum": ["income", "expense"], "description": "income=収入・売上、expense=支出・経費。"},
                "amount": {"type": "integer", "description": "金額（税込、円単位の整数）。"},
                "account_item_id": {"type": "integer", "description": "勘定科目ID（get_account_itemsで取得）。"},
                "partner_name": {"type": "string", "description": "取引先名。"},
                "description": {"type": "string", "description": "摘要・メモ。"},
                "tax_code": {"type": "integer", "description": "税区分コード。省略時は課税10%を自動設定。"},
            },
            "required": ["issue_date", "deal_type", "amount", "account_item_id"],
        },
    },
]


# ── freee トークン管理 ────────────────────────────────────────

def get_freee_token() -> str:
    with _token_lock:
        if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
            return _token_cache["access_token"]

        refresh_token = _token_cache.get("refresh_token") or FREEE_REFRESH_TOKEN_INITIAL
        if not refresh_token:
            raise RuntimeError("リフレッシュトークンがありません。freeeの再認証が必要です。")

        logger.info("freee アクセストークンを更新中...")
        resp = requests.post(
            FREEE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": FREEE_CLIENT_ID,
                "client_secret": FREEE_CLIENT_SECRET,
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        _token_cache["access_token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + data.get("expires_in", 21600)
        # 新しいリフレッシュトークンが発行された場合は更新する
        if data.get("refresh_token"):
            _token_cache["refresh_token"] = data["refresh_token"]

        save_token_cache(_token_cache)
        logger.info("freee トークン更新・保存完了")
        return _token_cache["access_token"]


def freee_get(path: str, params: dict) -> dict:
    token = get_freee_token()
    resp = requests.get(
        f"{FREEE_API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def freee_post(path: str, payload: dict) -> dict:
    token = get_freee_token()
    resp = requests.post(
        f"{FREEE_API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ── freee API ツール実装 ──────────────────────────────────────

def get_deals(start_date=None, end_date=None, deal_type=None) -> dict:
    today = date.today()
    params = {
        "company_id": FREEE_COMPANY_ID,
        "start_issue_date": start_date or today.replace(day=1).isoformat(),
        "end_issue_date": end_date or today.isoformat(),
        "limit": 100,
    }
    if deal_type:
        params["type"] = deal_type
    data = freee_get("/deals", params)
    deals = data.get("deals", [])
    return {
        "period": f"{params['start_issue_date']} ～ {params['end_issue_date']}",
        "total_count": len(deals),
        "summary": {
            "total_income": sum(d["amount"] for d in deals if d["type"] == "income"),
            "total_expense": sum(d["amount"] for d in deals if d["type"] == "expense"),
        },
        "deals": [
            {
                "id": d["id"],
                "date": d["issue_date"],
                "type": "収入" if d["type"] == "income" else "支出",
                "amount": d["amount"],
                "partner": d.get("partner_name") or "-",
                "memo": d.get("description") or "",
            }
            for d in deals
        ],
    }


def get_account_items(keyword=None) -> dict:
    data = freee_get("/account_items", {"company_id": FREEE_COMPANY_ID})
    items = data.get("account_items", [])
    if keyword:
        items = [i for i in items if keyword in i.get("name", "")]
    return {
        "account_items": [
            {"id": i["id"], "name": i["name"], "category": i.get("account_category", "")}
            for i in items
        ]
    }


def create_deal(issue_date, deal_type, amount, account_item_id,
                partner_name=None, description=None, tax_code=None) -> dict:
    if tax_code is None:
        tax_code = 1 if deal_type == "income" else 2
    payload = {
        "company_id": FREEE_COMPANY_ID,
        "issue_date": issue_date,
        "type": deal_type,
        "details": [{"tax_code": tax_code, "account_item_id": account_item_id,
                     "amount": amount, "description": description or ""}],
    }
    if partner_name:
        payload["partner_name"] = partner_name
    data = freee_post("/deals", {"deal": payload})
    deal = data.get("deal", {})
    return {
        "success": True,
        "id": deal.get("id"),
        "issue_date": deal.get("issue_date"),
        "type": "収入" if deal.get("type") == "income" else "支出",
        "amount": amount,
        "partner_name": partner_name or "-",
    }


def execute_tool(name: str, tool_input: dict) -> str:
    try:
        if name == "get_deals":
            return json.dumps(get_deals(**tool_input), ensure_ascii=False)
        if name == "get_account_items":
            return json.dumps(get_account_items(**tool_input), ensure_ascii=False)
        if name == "create_deal":
            return json.dumps(create_deal(**tool_input), ensure_ascii=False)
        return json.dumps({"error": f"Unknown tool: {name}"})
    except requests.HTTPError as e:
        logger.error("freee API error: %s %s", e.response.status_code, e.response.text)
        return json.dumps({"error": f"freee APIエラー: {e.response.status_code}"})
    except Exception as e:
        logger.error("Tool error: %s", e)
        return json.dumps({"error": str(e)})


# ── Claude エージェントループ ─────────────────────────────────

def ask_claude(user_message: str) -> str:
    today = date.today().strftime("%Y年%m月%d日")
    system = SYSTEM_PROMPT.format(today=today)
    messages = [{"role": "user", "content": user_message}]

    for _ in range(8):
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return ""

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info("Tool: %s %s", block.name, block.input)
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    return "申し訳ありません。回答を生成できませんでした。"


# ── LINE Webhook ──────────────────────────────────────────────

def reply_to_line(reply_token: str, text: str) -> None:
    with ApiClient(line_configuration) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text[:5000])],
            )
        )


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    logger.info("Webhook received")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.warning("Invalid LINE signature")
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event: MessageEvent) -> None:
    user_message = event.message.text
    logger.info("User: %s", user_message[:50])
    try:
        reply_text = ask_claude(user_message)
    except anthropic.APIError as e:
        logger.error("Claude error: %s", e)
        reply_text = "申し訳ありません。現在AIが応答できない状態です。"
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        reply_text = "エラーが発生しました。しばらくしてから再度お試しください。"
    try:
        reply_to_line(event.reply_token, reply_text)
    except Exception as e:
        logger.error("LINE reply error: %s", e)


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

#!/usr/bin/env python3
"""
LINE × Claude × freee 経理アシスタントBot
LINE Webhook → Claude API (tool_use) → freee API → LINE Reply
"""

import os
import json
import time
import logging
import threading
import requests
import anthropic
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
FREEE_REFRESH_TOKEN = os.environ["FREEE_REFRESH_TOKEN"]
FREEE_COMPANY_ID = int(os.environ["FREEE_COMPANY_ID"])

FREEE_TOKEN_URL = "https://accounts.secure.freee.co.jp/public_api/token"
FREEE_API_BASE = "https://api.freee.co.jp/api/1"

handler = WebhookHandler(LINE_CHANNEL_SECRET)
line_configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# freee アクセストークンのメモリキャッシュ
_token_cache: dict = {"access_token": None, "expires_at": 0}
_token_lock = threading.Lock()

SYSTEM_PROMPT = """あなたはLeo経理アシスタントです。freeeの会計データの照会をサポートします。
ユーザーから取引・売上・支出・経費などについて聞かれたら、get_deals ツールを使ってfreeeからデータを取得してください。
回答はLINEのチャット形式で読みやすく、簡潔にまとめてください。
金額は「円」単位でカンマ区切りで表示し、日付はわかりやすく表示してください。
今日の日付は {today} です。"""

TOOLS = [
    {
        "name": "get_deals",
        "description": "freeeから取引一覧を取得します。売上・支出・経費などの照会に使います。",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "取得開始日（YYYY-MM-DD形式）。省略時は今月1日。",
                },
                "end_date": {
                    "type": "string",
                    "description": "取得終了日（YYYY-MM-DD形式）。省略時は今日。",
                },
                "deal_type": {
                    "type": "string",
                    "enum": ["income", "expense"],
                    "description": "取引種別。income=収入、expense=支出。省略時は両方。",
                },
            },
            "required": [],
        },
    },
]


# ── freee トークン管理 ────────────────────────────────────────

def get_freee_token() -> str:
    with _token_lock:
        if time.time() < _token_cache["expires_at"] - 60:
            return _token_cache["access_token"]

        logger.info("freee access token を更新中...")
        resp = requests.post(
            FREEE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": FREEE_CLIENT_ID,
                "client_secret": FREEE_CLIENT_SECRET,
                "refresh_token": FREEE_REFRESH_TOKEN,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache["access_token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + data.get("expires_in", 21600)
        logger.info("freee access token 更新完了")
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


# ── freee API ツール実装 ──────────────────────────────────────

def get_deals(
    start_date: str = None,
    end_date: str = None,
    deal_type: str = None,
) -> dict:
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


def execute_tool(name: str, tool_input: dict) -> str:
    try:
        if name == "get_deals":
            result = get_deals(**tool_input)
            return json.dumps(result, ensure_ascii=False)
        return json.dumps({"error": f"Unknown tool: {name}"})
    except requests.HTTPError as e:
        logger.error("freee API error: %s", e)
        return json.dumps({"error": f"freee APIエラー: {e.response.status_code}"})
    except Exception as e:
        logger.error("Tool error: %s", e)
        return json.dumps({"error": str(e)})


# ── Claude エージェントループ ─────────────────────────────────

def ask_claude(user_message: str) -> str:
    today = date.today().strftime("%Y年%m月%d日")
    system = SYSTEM_PROMPT.format(today=today)

    messages = [{"role": "user", "content": user_message}]

    for _ in range(5):  # ツール呼び出しは最大5回まで
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
        logger.info("Reply sent")
    except Exception as e:
        logger.error("LINE reply error: %s", e)


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

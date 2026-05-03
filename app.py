#!/usr/bin/env python3
"""
LINE × Claude 経理アシスタントBot
LINE Webhook → Claude API → LINE Reply
"""

import os
import logging
import anthropic
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

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

handler = WebhookHandler(LINE_CHANNEL_SECRET)
line_configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """あなたはLeo経理アシスタントです。freeeの会計データの照会や取引登録をサポートします。
ユーザーからの質問に対して、わかりやすく丁寧に回答してください。
回答は簡潔にまとめ、LINEのチャット形式で読みやすくしてください。"""


def ask_claude(user_message: str) -> str:
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def reply_to_line(reply_token: str, text: str) -> None:
    with ApiClient(line_configuration) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
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
    logger.info("User message: %s", user_message[:50])

    try:
        reply_text = ask_claude(user_message)
    except anthropic.APIError as e:
        logger.error("Claude API error: %s", e)
        reply_text = "申し訳ありません。現在AIが応答できない状態です。しばらくしてから再度お試しください。"

    try:
        reply_to_line(event.reply_token, reply_text)
        logger.info("Reply sent successfully")
    except Exception as e:
        logger.error("LINE reply error: %s", e)


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

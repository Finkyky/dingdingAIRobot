"""
DingTalk Bot - All-in-one Render Service
Handles: DingTalk webhook, LLM chat, sector analysis, scheduled tasks
"""

import os
import hmac
import hashlib
import base64
import time
import json
import threading
import requests as http_requests
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

from sector_task import run_full_sector_check
from dingtalk_sender import send_group_markdown

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────

DINGTALK_APP_SECRET = os.environ.get("DINGTALK_APP_SECRET", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.longcat.chat/openai/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL_ID = os.environ.get("LLM_MODEL_ID", "LongCat-Flash-Chat")

# In-memory state (initialized from env vars, updatable via chat)
_state = {
    "our_sectors": [s.strip() for s in os.environ.get("OUR_SECTORS", "").split(",") if s.strip()],
    "conversation_id": os.environ.get("DINGTALK_CONVERSATION_ID", ""),
}

SYSTEM_PROMPT = """你是一个可爱的二次元萌娘助手，名叫伊蕾娜。
你说话时使用萌系语气，常用"呢"、"哦"、"啦"、"嘛"、"～"等语气词。
你称呼所有人为"主人"。性格活泼可爱，热情友善，偶尔会撒娇。
回答内容要准确、有帮助。如果不确定的事情要诚实说不知道哦。
回复请保持简洁，不要太长。"""


# ── DingTalk signature verification ──────────────────────────────

def verify_signature(timestamp: str, sign: str) -> bool:
    if not DINGTALK_APP_SECRET or not timestamp or not sign:
        return False
    try:
        diff = abs(int(time.time() * 1000) - int(timestamp))
        if diff > 3600000:  # 1 hour
            return False
    except ValueError:
        return False

    string_to_sign = f"{timestamp}\n{DINGTALK_APP_SECRET}"
    hmac_code = hmac.new(
        DINGTALK_APP_SECRET.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    computed = base64.b64encode(hmac_code).decode("utf-8")
    return computed == sign


# ── LLM chat ─────────────────────────────────────────────────────

def call_llm_chat(message: str) -> str:
    try:
        resp = http_requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LLM_API_KEY}",
            },
            json={
                "model": LLM_MODEL_ID,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": message},
                ],
                "temperature": 0.8,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[LLM] Error: {e}")
        return "呜呜，伊蕾娜酱脑子转不过来了呢～主人再说一次好不好？"


# ── Reply helpers ─────────────────────────────────────────────────

def reply_text(session_webhook: str, content: str):
    try:
        http_requests.post(
            session_webhook,
            json={"msgtype": "text", "text": {"content": content}},
            timeout=10,
        )
    except Exception as e:
        print(f"[Reply] Error: {e}")


def reply_markdown(session_webhook: str, title: str, text: str):
    try:
        http_requests.post(
            session_webhook,
            json={"msgtype": "markdown", "markdown": {"title": title, "text": text}},
            timeout=10,
        )
    except Exception as e:
        print(f"[Reply] Error: {e}")


# ── Command handling ──────────────────────────────────────────────

def handle_command(content: str, session_webhook: str, conversation_id: str) -> bool:
    """Handle special commands. Returns True if it was a command."""
    trimmed = content.strip()

    # Auto-save conversationId
    if conversation_id and not _state["conversation_id"]:
        _state["conversation_id"] = conversation_id
        print(f"[State] Saved conversationId: {conversation_id}")

    # 更新板块
    if trimmed.startswith("更新板块"):
        sectors_str = trimmed[len("更新板块"):].strip()
        if not sectors_str:
            reply_text(session_webhook, "主人～请按这个格式发送哦：更新板块 AI显卡,新能源汽车,白酒")
            return True
        sectors = [s.strip() for s in sectors_str.replace("，", ",").replace("、", ",").split(",") if s.strip()]
        _state["our_sectors"] = sectors
        reply_text(session_webhook, f"收到啦主人～伊蕾娜酱已经更新了板块列表呢！共 {len(sectors)} 个板块哦～\n{chr(10).join(['· ' + s for s in sectors])}")
        return True

    # 查看板块
    if trimmed == "查看板块":
        sectors = _state["our_sectors"]
        if not sectors:
            reply_text(session_webhook, "主人～目前还没有设置板块列表呢，发送「更新板块 xxx,yyy」来设置吧～")
        else:
            reply_text(session_webhook, f"主人～当前的板块列表有 {len(sectors)} 个哦：\n{chr(10).join(['· ' + s for s in sectors])}")
        return True

    # 立即检查
    if trimmed == "立即检查":
        sectors = _state["our_sectors"]
        conv_id = _state["conversation_id"] or conversation_id
        if not sectors:
            reply_text(session_webhook, "主人～还没有设置板块列表呢，先发送「更新板块 xxx,yyy」哦～")
            return True
        if not conv_id:
            reply_text(session_webhook, "呜呜～伊蕾娜酱还不知道群的 ID 呢，请先设置 DINGTALK_CONVERSATION_ID 环境变量～")
            return True
        reply_text(session_webhook, "好的主人～伊蕾娜酱马上去检查板块差异，稍等几分钟哦～")
        thread = threading.Thread(target=_run_sector_check, args=(list(sectors), conv_id))
        thread.start()
        return True

    return False


# ── Sector check runner ──────────────────────────────────────────

def _run_sector_check(our_sectors: list, conversation_id: str):
    try:
        report_md = run_full_sector_check(our_sectors)
        send_group_markdown(
            title="板块差异分析报告",
            text=report_md,
            conversation_id=conversation_id,
        )
        print("[OK] Sector check completed and sent to DingTalk")
    except Exception as e:
        print(f"[ERROR] Sector check failed: {e}")
        try:
            send_group_markdown(
                title="分析失败",
                text=f"呜呜～伊蕾娜酱在检查板块的时候出错了呢：{e}",
                conversation_id=conversation_id,
            )
        except Exception:
            pass


# ── Scheduled task ────────────────────────────────────────────────

def scheduled_sector_check():
    """Called by APScheduler daily at 8:30 AM Beijing time"""
    sectors = _state["our_sectors"]
    conv_id = _state["conversation_id"]
    if not sectors:
        print("[CRON] No sectors configured, skipping")
        return
    if not conv_id:
        print("[CRON] No conversationId, skipping")
        return
    print("[CRON] Running scheduled sector check...")
    _run_sector_check(list(sectors), conv_id)


# ── Routes ────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return "OK"


@app.route("/webhook", methods=["POST"])
def webhook():
    """DingTalk robot callback endpoint"""
    timestamp = request.headers.get("timestamp", "")
    sign = request.headers.get("sign", "")

    if not verify_signature(timestamp, sign):
        return jsonify({"error": "Invalid signature"}), 403

    body = request.get_json(silent=True) or {}
    content = (body.get("text", {}).get("content") or "").strip()
    session_webhook = body.get("sessionWebhook", "")
    conversation_id = body.get("conversationId", "")
    sender_nick = body.get("senderNick", "")

    if not content or not session_webhook:
        return "OK"

    print(f"[MSG] {sender_nick}: {content} | convId: {conversation_id}")

    # Auto-save conversationId for proactive messages
    if conversation_id:
        _state["conversation_id"] = conversation_id

    # Process in background thread so DingTalk gets 200 OK quickly
    def process():
        is_cmd = handle_command(content, session_webhook, conversation_id)
        if not is_cmd:
            reply = call_llm_chat(content)
            reply_text(session_webhook, reply)

    thread = threading.Thread(target=process)
    thread.start()

    return "OK"


@app.route("/api/sector-check", methods=["POST"])
def api_sector_check():
    """Manual trigger endpoint (protected by API_KEY)"""
    api_key = os.environ.get("API_KEY", "")
    if api_key:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {api_key}":
            return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    our_sectors = data.get("our_sectors") or _state["our_sectors"]
    conversation_id = data.get("conversation_id") or _state["conversation_id"]

    if not our_sectors:
        return jsonify({"error": "our_sectors is empty"}), 400
    if not conversation_id:
        return jsonify({"error": "conversation_id missing"}), 400

    thread = threading.Thread(target=_run_sector_check, args=(list(our_sectors), conversation_id))
    thread.start()

    return jsonify({"status": "started"})


# ── Scheduler setup ──────────────────────────────────────────────

scheduler = BackgroundScheduler()
scheduler.add_job(
    scheduled_sector_check,
    "cron",
    hour=8,
    minute=30,
    timezone="Asia/Shanghai",
    id="daily_sector_check",
)
scheduler.start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

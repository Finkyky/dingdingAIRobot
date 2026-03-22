"""
DingTalk message sender via OpenAPI
Sends proactive messages to group using enterprise internal robot
"""

import os
import json
import requests

DINGTALK_APP_KEY = os.environ.get("DINGTALK_APP_KEY", "")
DINGTALK_APP_SECRET = os.environ.get("DINGTALK_APP_SECRET", "")


def get_access_token() -> str:
    """Get DingTalk access token (valid 2 hours)"""
    url = "https://oapi.dingtalk.com/gettoken"
    resp = requests.get(url, params={
        "appkey": DINGTALK_APP_KEY,
        "appsecret": DINGTALK_APP_SECRET,
    }, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode") != 0:
        raise Exception(f"DingTalk gettoken failed: {data.get('errmsg')}")
    return data["access_token"]


def send_group_markdown(title: str, text: str, conversation_id: str) -> None:
    """Send markdown message to DingTalk group via robot OpenAPI"""
    token = get_access_token()
    url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
    headers = {
        "x-acs-dingtalk-access-token": token,
        "Content-Type": "application/json",
    }
    body = {
        "robotCode": DINGTALK_APP_KEY,
        "openConversationId": conversation_id,
        "msgKey": "sampleMarkdown",
        "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False),
    }
    resp = requests.post(url, headers=headers, json=body, timeout=10)
    if resp.status_code != 200:
        raise Exception(f"DingTalk send failed [{resp.status_code}]: {resp.text}")
    print(f"[DingTalk] Message sent: {title}")

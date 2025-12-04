import base64
import json
import os
import textwrap
import requests
from datetime import datetime
from flask import Flask, render_template, request
from openai import OpenAI

# ====★ OpenAI APIキー（Railway用：環境変数から読む）★====
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
# ============================================================

client = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)

CONTEXT_FILE = "global_context.json"


# --------- 共通ユーティリティ ---------
def load_context():
    if not os.path.exists(CONTEXT_FILE):
        return None
    try:
        with open(CONTEXT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_context(summary: str):
    data = {
        "summary": summary,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(CONTEXT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def to_data_url(img_bytes: bytes) -> str:
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:image/png;base64,{b64}"


# --------- 環境（日足＋追加資料）を更新する処理 ---------
def build_environment_summary(daily_bytes, extra_imgs, urls, memo_text):

    contents = []

    base_instruction = """あなたは日本株のトレード環境アナリストです。
ユーザーがアップロードした「日足チャート」「材料画像」「URL」「メモ」から、
当面の相場環境を短く要約してください。

必ず次の JSON 形式のみで回答：
{
  "summary": "日本語で200文字以内の要約"
}
"""
    contents.append({"type": "input_text", "text": base_instruction})

    # 日足画像
    if daily_bytes:
        contents.append({"type": "input_text", "text": "▼日足チャート"})
        contents.append({"type": "input_image", "image_url": to_data_url(daily_bytes)})

    # 追加画像
    for idx, b in enumerate(extra_imgs, start=1):
        contents.append({"type": "input_text", "text": f"▼追加画像 {idx}"})
        contents.append({"type": "input_image", "image_url": to_data_url(b)})

    # URL
    cleaned_urls = [u.strip() for u in urls if u.strip()]
    for idx, url in enumerate(cleaned_urls, start=1):
        try:
            resp = requests.get(url, timeout=5)
            snippet = textwrap.shorten(resp.text, width=2000, placeholder="...")
        except Exception as e:
            snippet = f"(URL取得エラー: {e})"

        contents.append(
            {
                "type": "input_text",
                "text": f"▼URL {idx}\n{url}\n抜粋:\n{snippet}",
            }
        )

    # メモ
    memo_text = memo_text.strip()
    if memo_text:
        contents.append({"type": "input_text", "text": f"メモ:\n{memo_text}"})

    # GPT要約生成
    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[{"role": "user", "content": contents}],
        max_output_tokens=400,
    )

    try:
        raw = response.output[0].content[0].text
        data = json.loads(raw)
        summary = data.get("summary", "").strip()
        if not summary:
            summary = "要約生成に失敗しました。"
    except Exception:
        summary = "要約解析に失敗しました。"

    return summary


# --------- 個別の5分足＋板を判定する処理 ---------
def analyze_intraday(chart_bytes, board_bytes, memo_text, env_summary):

    contents = []

    head = """あなたは日本株の短期トレードアドバイザーです。
以下の情報を元に超短期のアクション（数分〜数十分）を判断してください。

必ず次のJSON形式のみで回答：
{
  "action": "buy | sell | hold",
  "reason": "日本語で100文字以内"
}
"""
    contents.append({"type": "input_text", "text": head})

    # 環境
    if env_summary:
        contents.append(
            {"type": "input_text", "text": f"【登録済み環境要約】\n{env_summary}"}
        )

    # 5分足
    contents.append({"type": "input_text", "text": "▼5分足チャート"})
    contents.append({"type": "input_image", "image_url": to_data_url(chart_bytes)})

    # 板
    contents.append({"type": "input_text", "text": "▼板"})
    contents.append({"type": "input_image", "image_url": to_data_url(board_bytes)})

    # メモ
    memo_text = memo_text.strip()
    if memo_text:
        contents.append({"type": "input_text", "text": f"メモ:\n{memo_text}"})

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[{"role": "user", "content": contents}],
        max_output_tokens=300,
    )

    try:
        raw = response.output[0].content[0].text
        data = json.loads(raw)
        action = data.get("action", "hold").lower()
        reason = data.get("reason", "理由なし")
    except Exception:
        action = "hold"
        reason = "解析エラーのため HOLD としました。"

    if action not in ["buy", "sell", "hold"]:
        action = "hold"

    return action.upper(), reason


# --------- Flaskルート ---------
@app.route("/", methods=["GET", "POST"])
def index():
    env = load_context()
    env_summary = env["summary"] if env else None
    env_updated_at = env["updated_at"] if env else None

    result = None
    env_message = None
    error = None

    if request.method == "POST":
        mode = request.form.get("mode")

        # --- 環境更新 ---
        if mode == "update_env":
            daily_file = request.files.get("daily_image")
            extra_files = request.files.getlist("extra_images")
            urls_text = request.form.get("env_urls", "")
            env_memo = request.form.get("env_memo", "")

            daily_bytes = daily_file.read() if daily_file and daily_file.filename else None
            extra_imgs = [f.read() for f in extra_files if f and f.filename]
            urls = urls_text.splitlines()

            try:
                summary = build_environment_summary(
                    daily_bytes,
                    extra_imgs,
                    urls,
                    env_memo,
                )
                save_context(summary)
                env_summary = summary
                env_updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                env_message = "環境情報を更新しました。"
            except Exception as e:
                error = f"環境更新エラー: {e}"

        # --- 判定 ---
        elif mode == "judge":
            chart_file = request.files.get("chart_image")
            board_file = request.files.get("board_image")
            memo_text = request.form.get("memo", "")

            if not chart_file or not chart_file.filename:
                error = "5分足チャートを選択してください。"
            elif not board_file or not board_file.filename:
                error = "板画像を選択してください。"
            else:
                try:
                    chart_bytes = chart_file.read()
                    board_bytes = board_file.read()

                    action, reason = analyze_intraday(
                        chart_bytes,
                        board_bytes,
                        memo_text,
                        env_summary,
                    )
                    result = {"action": action, "reason": reason}
                except Exception as e:
                    error = f"判定エラー: {e}"

    return render_template(
        "index.html",
        result=result,
        error=error,
        env_summary=env_summary,
        env_updated_at=env_updated_at,
        env_message=env_message,
    )


if __name__ == "__main__":
    # RailwayはPORTを環境変数で渡すので自動取得
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

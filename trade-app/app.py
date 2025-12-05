import base64
import json
import os
import textwrap
from datetime import datetime

from flask import Flask, render_template, request
from openai import OpenAI

# ========= 自分の OpenAI APIキー =========
OPENAI_API_KEY = "sk-ここに自分のキー"
# ======================================

client = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)

CONTEXT_FILE = "contexts.json"


# ---------- 共通ユーティリティ ----------

def load_contexts():
    """銘柄ごとの環境情報を読み込み（JSON が無ければ空 dict）"""
    if not os.path.exists(CONTEXT_FILE):
        return {}
    try:
        with open(CONTEXT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        return {}


def save_contexts(contexts: dict):
    """銘柄ごとの環境情報を保存"""
    with open(CONTEXT_FILE, "w", encoding="utf-8") as f:
        json.dump(contexts, f, ensure_ascii=False, indent=2)


def to_data_url(img_bytes: bytes) -> str:
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:image/png;base64,{b64}"


# ---------- GPT: 環境要約を作る ----------

def build_environment_summary(
    symbol: str,
    name: str,
    daily_bytes: bytes | None,
    extra_imgs: list[bytes],
    urls: list[str],
    memo_text: str,
):
    contents = []

    base_instruction = f"""あなたは日本株トレード環境アナリストです。
銘柄コード: {symbol}
銘柄名: {name or "（未入力）"}

ユーザーがアップロードした「日足チャート」「材料に関する画像」「URL」「メモ」から、
この銘柄の当面の相場環境を日本語で要約してください。

・日足チャート: 中期トレンド、重要なサポート/レジスタンス
・追加画像: ニュース、ランキングなど
・URL: IR資料やニュース本文
・メモ: ユーザーの補足

出力フォーマットは次の JSON のみとします:

{{
  "summary": "日本語で200文字以内の要約（地合い・トレンド・材料など）"
}}
"""
    contents.append({"type": "input_text", "text": base_instruction})

    if daily_bytes:
        contents.append(
            {
                "type": "input_text",
                "text": "▼これは日足チャート画像です。中期トレンドを読み取ってください。",
            }
        )
        contents.append({"type": "input_image", "image_url": to_data_url(daily_bytes)})

    for idx, b in enumerate(extra_imgs, start=1):
        contents.append(
            {
                "type": "input_text",
                "text": f"▼追加参考画像 {idx}。ニュースやランキング情報などです。",
            }
        )
        contents.append({"type": "input_image", "image_url": to_data_url(b)})

    cleaned_urls = [u.strip() for u in urls if u.strip()]
    for idx, url in enumerate(cleaned_urls, start=1):
        snippet = ""
        try:
            import requests

            resp = requests.get(url, timeout=5)
            text = resp.text
            snippet = textwrap.shorten(text, width=2000, placeholder="...")
        except Exception as e:
            snippet = f"(URL取得エラー: {e})"

        contents.append(
            {
                "type": "input_text",
                "text": f"▼URL {idx}: {url}\nこのページの内容要約に使ってください。\n抜粋テキスト:\n{snippet}",
            }
        )

    memo_text = memo_text.strip()
    if memo_text:
        contents.append(
            {
                "type": "input_text",
                "text": f"ユーザーからのメモ:\n{memo_text}",
            }
        )

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[{"role": "user", "content": contents}],
        max_output_tokens=400,
    )

    try:
        output_text = response.output[0].content[0].text
        data = json.loads(output_text)
        summary = str(data.get("summary", "")).strip()
        if not summary:
            summary = "要約生成に失敗しましたが、環境情報として利用してください。"
    except Exception:
        summary = "要約の解析に失敗しました。環境情報の解釈はAIに任せてください。"

    return summary


# ---------- GPT: 5分足＋板から判定 ----------

def analyze_intraday(
    chart_bytes: bytes,
    board_bytes: bytes,
    memo_text: str,
    env_summary: str | None,
):
    contents = []

    base_instruction = """あなたは日本株の短期トレードアドバイザーです。
以下の情報を総合して、数分〜数十分スパンのアクションを判断してください。

必ず次の3択から1つだけ選んでください:
- "buy"  : 新規買い or 追加エントリーが有利そう
- "sell" : 利確 or 損切りを含む売りが妥当そう
- "hold" : まだ様子見が無難

また、可能であれば「どの価格帯を狙うと良さそうか」もシンプルに示してください。

出力フォーマットは次の JSON のみ:

{
  "action": "buy | sell | hold",
  "reason": "日本語で150文字以内の理由",
  "price_hint": "日本語で簡潔な価格帯コメント（例：1550〜1570円で押し目買いを検討）"
}
"""
    contents.append({"type": "input_text", "text": base_instruction})

    if env_summary:
        contents.append(
            {
                "type": "input_text",
                "text": f"【事前に登録された環境・材料の要約】\n{env_summary}",
            }
        )

    contents.append(
        {
            "type": "input_text",
            "text": "▼これは5分足チャートです。直近の値動きと出来高から短期トレンドを判断してください。",
        }
    )
    contents.append({"type": "input_image", "image_url": to_data_url(chart_bytes)})

    contents.append(
        {
            "type": "input_text",
            "text": "▼これは現在の気配値（板）の画像です。売り板・買い板の厚さや偏りを判断に使ってください。",
        }
    )
    contents.append({"type": "input_image", "image_url": to_data_url(board_bytes)})

    memo_text = memo_text.strip()
    if memo_text:
        contents.append(
            {
                "type": "input_text",
                "text": f"ユーザーからの補足メモ:\n{memo_text}",
            }
        )

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[{"role": "user", "content": contents}],
        max_output_tokens=400,
    )

    try:
        output_text = response.output[0].content[0].text
        data = json.loads(output_text)
        action = str(data.get("action", "hold")).lower()
        reason = str(data.get("reason", "理由なし"))
        price_hint = str(data.get("price_hint", "")).strip()
    except Exception:
        action = "hold"
        reason = "出力解析エラーのため様子見としました。"
        price_hint = ""

    if action not in ["buy", "sell", "hold"]:
        action = "hold"

    return action.upper(), reason, price_hint


# ---------- ルーティング ----------

@app.route("/", methods=["GET", "POST"])
def index():
    contexts = load_contexts()

    # 登録済み銘柄一覧
    registered_list = []
    for code, info in contexts.items():
        registered_list.append(
            {
                "code": code,
                "name": info.get("name", ""),
                "updated_at": info.get("updated_at", ""),
            }
        )
    registered_list.sort(key=lambda x: x["code"])

    result = None
    error = None
    env_message = None
    selected_code = None

    # URL パラメータ ?code=9984 でプリセット
    selected_code = request.args.get("code", "").strip() or None

    if request.method == "POST":
        form_type = request.form.get("form_type")
        if form_type == "judge":
            # ① 5分足＋板 判定フォーム
            symbol = request.form.get("judge_symbol", "").strip()
            selected_code = symbol or selected_code

            if not symbol:
                error = "銘柄コードを入力してください。"
            else:
                chart_file = request.files.get("chart_image")
                board_file = request.files.get("board_image")
                memo_text = request.form.get("judge_memo", "")

                if not chart_file or not chart_file.filename:
                    error = "5分足チャート画像を選択してください。"
                elif not board_file or not board_file.filename:
                    error = "気配値（板）の画像を選択してください。"
                else:
                    try:
                        chart_bytes = chart_file.read()
                        board_bytes = board_file.read()
                        ctx = contexts.get(symbol)
                        env_summary = ctx.get("summary") if ctx else None
                        action, reason, price_hint = analyze_intraday(
                            chart_bytes, board_bytes, memo_text, env_summary
                        )
                        result = {
                            "symbol": symbol,
                            "action": action,
                            "reason": reason,
                            "price_hint": price_hint,
                        }
                    except Exception as e:
                        error = f"判定中にエラーが発生しました: {e}"

        elif form_type == "env":
            # ② 環境登録フォーム
            symbol = request.form.get("env_symbol", "").strip()
            name = request.form.get("env_name", "").strip()
            selected_code = symbol or selected_code

            if not symbol:
                error = "銘柄コード（環境登録用）は必須です。"
            else:
                daily_file = request.files.get("daily_image")
                extra_files = request.files.getlist("extra_images")
                urls_text = request.form.get("env_urls", "")
                env_memo = request.form.get("env_memo", "")

                daily_bytes = (
                    daily_file.read() if daily_file and daily_file.filename else None
                )
                extra_imgs = [
                    f.read() for f in extra_files if f and f.filename
                ]
                urls = urls_text.splitlines()

                try:
                    summary = build_environment_summary(
                        symbol=symbol,
                        name=name,
                        daily_bytes=daily_bytes,
                        extra_imgs=extra_imgs,
                        urls=urls,
                        memo_text=env_memo,
                    )

                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    contexts[symbol] = {
                        "name": name,
                        "summary": summary,
                        "updated_at": now_str,
                    }
                    save_contexts(contexts)
                    env_message = f"{symbol} の環境情報を更新しました。"
                except Exception as e:
                    error = f"環境更新中にエラーが発生しました: {e}"

    return render_template(
        "index.html",
        result=result,
        error=error,
        env_message=env_message,
        registered_list=registered_list,
        selected_code=selected_code or "",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

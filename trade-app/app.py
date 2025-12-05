import base64
import json
import os
from datetime import datetime
from typing import Dict, Any, List, Optional

from flask import Flask, render_template, request
from openai import OpenAI

app = Flask(__name__)

CONTEXT_FILE = "contexts.json"

# ---------- OpenAI クライアント設定 ----------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    # Render に環境変数を入れてない場合は起動時に落としておく
    raise RuntimeError("OPENAI_API_KEY が環境変数に設定されていません。Render の Environment を確認してください。")

client = OpenAI(api_key=OPENAI_API_KEY)


# ---------- 環境データの保存 / 読み込み ----------

def load_contexts() -> Dict[str, Any]:
    """contexts.json から銘柄ごとの環境データを読む"""
    if not os.path.exists(CONTEXT_FILE):
        return {}
    try:
        with open(CONTEXT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_contexts(contexts: Dict[str, Any]) -> None:
    """銘柄ごとの環境データを contexts.json に保存"""
    with open(CONTEXT_FILE, "w", encoding="utf-8") as f:
        json.dump(contexts, f, ensure_ascii=False, indent=2)


def img_bytes_to_data_url(img_bytes: bytes) -> str:
    """画像バイト列 → data URL"""
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:image/png;base64,{b64}"


# ---------- OpenAI での判定ロジック ----------

def call_gpt_for_intraday(
    symbol: str,
    env_summary: Optional[str],
    chart_img_bytes: bytes,
    board_img_bytes: bytes,
    memo_text: str,
) -> Dict[str, Any]:
    """
    5分足 + 板画像 + メモ + （あれば）環境サマリを投げて、アクション案を返す
    """
    system_msg = (
        "あなたは日本株の短期トレードを手伝うアシスタントです。"
        "与えられた 5 分足チャートと板のスクショから、"
        "直近 1〜2 時間程度のエントリー／利確／損切りの方針をわかりやすく提案してください。"
        "数値は『だいたいの目安』でよいですが、レンジを具体的に示してください。"
    )

    user_text_parts = [
        f"銘柄コード: {symbol}",
    ]

    if env_summary:
        user_text_parts.append("【この銘柄の環境メモ】")
        user_text_parts.append(env_summary)

    if memo_text.strip():
        user_text_parts.append("【トレーダーからの補足メモ】")
        user_text_parts.append(memo_text.strip())

    user_text_parts.append(
        "これらを踏まえて、以下の形式で日本語で出力してください。\n\n"
        "1. 現在の状況の要約（テクニカル + 板の雰囲気）\n"
        "2. 短期アクション\n"
        "   - 今の推奨アクション（例: 見送り / 押し目買い / 逆張り買い / 利確売り など）\n"
        "   - もし『買い』なら：狙いたい買いレンジ（〇〇〜〇〇円）\n"
        "   - もし『売り』なら：利確を意識したい売りレンジ（〇〇〜〇〇円）\n"
        "   - 損切りを検討すべきラインの目安\n"
        "3. 上記アクションを選んだ理由を、チャートと板のどの点からそう判断したのか具体的に\n"
        "4. 注意点・無理して入らない方がよいパターンがあればその条件\n"
    )

    user_text = "\n".join(user_text_parts)

    chart_data_url = img_bytes_to_data_url(chart_img_bytes)
    board_data_url = img_bytes_to_data_url(board_img_bytes)

    resp = client.responses.create(
        model="gpt-4o-mini",
        input=[
            {"role": "system", "content": [{"type": "text", "text": system_msg}]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "input_image", "image_url": {"url": chart_data_url}},
                    {"type": "input_image", "image_url": {"url": board_data_url}},
                ],
            },
        ],
    )

    ai_text = resp.output[0].content[0].text.value

    # ここではシンプルに「アクション種別」だけ軽く抽出しておく
    action = "様子見"
    action_lower = ai_text.lower()
    if "買" in ai_text and "売" not in ai_text:
        action = "買い検討"
    elif "利確" in ai_text or "手仕舞い" in ai_text or "売り" in ai_text:
        action = "売り検討"

    return {
        "symbol": symbol,
        "action": action,
        "full_text": ai_text,
    }


# ---------- Flask ルート ----------

@app.route("/", methods=["GET", "POST"])
def index():
    contexts = load_contexts()
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    env_message: Optional[str] = None

    judge_symbol = ""
    env_symbol = ""

    if request.method == "POST":
        form_type = request.form.get("form_type")  # ★どのフォームかを判定

        # --- ① 判定フォーム ---
        if form_type == "judge":
            judge_symbol = request.form.get("judge_symbol", "").strip()

            if not judge_symbol:
                error = "銘柄コード（判定対象）を入力してください。"
            else:
                chart_file = request.files.get("chart_image")
                board_file = request.files.get("board_image")
                memo_text = request.form.get("judge_memo", "")

                if not chart_file or chart_file.filename == "":
                    error = "5分足チャート画像を選択してください。"
                elif not board_file or board_file.filename == "":
                    error = "気配値（板）の画像を選択してください。"
                else:
                    try:
                        chart_bytes = chart_file.read()
                        board_bytes = board_file.read()

                        env_ctx = contexts.get(judge_symbol)
                        env_summary = env_ctx.get("summary") if isinstance(env_ctx, dict) else None

                        result = call_gpt_for_intraday(
                            symbol=judge_symbol,
                            env_summary=env_summary,
                            chart_img_bytes=chart_bytes,
                            board_img_bytes=board_bytes,
                            memo_text=memo_text,
                        )
                    except Exception as e:
                        error = f"判定中にエラーが発生しました: {e}"

        # --- ② 環境登録フォーム ---
        elif form_type == "env":
            env_symbol = request.form.get("env_symbol", "").strip()
            env_name = request.form.get("env_name", "").strip()
            env_urls_raw = request.form.get("env_urls", "")
            env_memo = request.form.get("env_memo", "")

            if not env_symbol:
                error = "銘柄コード（環境登録用）は必須です。"
            else:
                daily_file = request.files.get("daily_image")
                extra_files = request.files.getlist("extra_images")

                daily_b64 = None
                if daily_file and daily_file.filename:
                    daily_b64 = img_bytes_to_data_url(daily_file.read())

                extra_b64_list: List[str] = []
                for f in extra_files:
                    if f and f.filename:
                        extra_b64_list.append(img_bytes_to_data_url(f.read()))

                urls: List[str] = []
                for line in env_urls_raw.splitlines():
                    u = line.strip()
                    if u:
                        urls.append(u)

                summary_parts = []
                if env_memo.strip():
                    summary_parts.append("【環境メモ】")
                    summary_parts.append(env_memo.strip())
                if urls:
                    summary_parts.append("【参考URL】")
                    for u in urls:
                        summary_parts.append(f"- {u}")

                summary = "\n".join(summary_parts) if summary_parts else ""

                ctx = {
                    "symbol": env_symbol,
                    "name": env_name,
                    "summary": summary,
                    "urls": urls,
                    "memo": env_memo,
                    "daily_image": daily_b64,
                    "extra_images": extra_b64_list,
                    "updated_at": datetime.utcnow().isoformat(),
                }

                contexts[env_symbol] = ctx
                try:
                    save_contexts(contexts)
                    env_message = f"{env_symbol} の環境情報を更新しました。"
                except Exception as e:
                    error = f"環境情報の保存に失敗しました: {e}"

    # GET または POST 後の画面表示
    return render_template(
        "index.html",
        contexts=contexts,
        result=result,
        error=error,
        env_message=env_message,
        judge_symbol=judge_symbol,
        env_symbol=env_symbol,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

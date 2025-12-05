import base64
import json
import os
import textwrap
from datetime import datetime

import requests
from flask import Flask, render_template, request
from openai import OpenAI

# ====★ 自分の OpenAI APIキーを入れる ★====
OPENAI_API_KEY = "sk-ここに自分のキーを貼る"
# =========================================

client = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)

# 銘柄ごとの環境情報を保存するファイル
ENV_FILE = "symbol_envs.json"


# ---------------- 共通ユーティリティ ----------------
def load_all_envs():
    """全銘柄の環境情報を読み込む"""
    if not os.path.exists(ENV_FILE):
        return {}
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 期待フォーマット: { "9434": {"name": "ソフトバンク", "summary": "...", "updated_at": "..."}, ... }
        return data
    except Exception:
        return {}


def save_all_envs(envs: dict):
    """全銘柄の環境情報を書き出す"""
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        json.dump(envs, f, ensure_ascii=False, indent=2)


def to_data_url(img_bytes: bytes) -> str:
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:image/png;base64,{b64}"


# ---------------- 環境（日足＋材料など）の要約生成 ----------------
def build_environment_summary(
    daily_bytes: bytes | None,
    extra_imgs: list[bytes],
    urls: list[str],
    memo_text: str,
    symbol_code: str,
    symbol_name: str,
):
    """
    日足画像（任意）、追加画像複数、URL群、テキストメモを GPT に渡して
    「その銘柄の環境要約」を作る。
    """
    contents = []

    base_instruction = f"""あなたは日本株のトレード環境アナリストです。
対象銘柄は「{symbol_code} {symbol_name}」です。

ユーザーがアップロードした「日足チャート」「材料に関する画像」「URL」「テキストメモ」から、
その銘柄の当面の相場環境を日本語で簡潔に要約してください。

・日足チャート: 中期〜数日スパンのトレンド（上昇/下降/レンジ）、移動平均との位置など
・追加画像: ニュース、ランキング情報など
・URL: IR資料やニュース記事などのテキスト情報
・メモ: ユーザーの補足コメント

出力フォーマットは、必ず次の JSON 形式だけにしてください:

{{
  "summary": "日本語で200文字以内の要約（地合い、日足トレンド、材料の方向性など）"
}}

余計な文章やコメントは一切書かず、必ずこの JSON だけを返してください。
"""
    contents.append({"type": "input_text", "text": base_instruction})

    # 日足画像
    if daily_bytes:
        contents.append(
            {
                "type": "input_text",
                "text": "▼これは日足チャートの画像です。中期トレンドや重要なサポート/レジスタンスを読み取ってください。",
            }
        )
        contents.append({"type": "input_image", "image_url": to_data_url(daily_bytes)})

    # 追加画像
    for idx, b in enumerate(extra_imgs, start=1):
        contents.append(
            {
                "type": "input_text",
                "text": f"▼追加参考画像 {idx}。ニュースやランキングなど、環境把握に役立つ情報です。",
            }
        )
        contents.append({"type": "input_image", "image_url": to_data_url(b)})

    # URLの中身を取得してテキストとして渡す
    cleaned_urls = [u.strip() for u in urls if u.strip()]
    for idx, url in enumerate(cleaned_urls, start=1):
        snippet = ""
        try:
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

    # メモ
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


# ---------------- 5分足＋板の短期アクション判定 ----------------
def analyze_intraday(
    chart_bytes: bytes,
    board_bytes: bytes,
    memo_text: str,
    env_summary: str | None,
    symbol_code: str,
    symbol_name: str | None,
):
    """
    5分足チャート＋板＋（任意）環境要約を元に、BUY/SELL/HOLD と
    それぞれの狙い目価格帯を判定する。
    """
    contents = []

    base_instruction = f"""あなたは日本株の短期トレードアドバイザーです。
対象銘柄は「{symbol_code} {symbol_name or ''}」です。

以下の情報を総合的に使って、ごく短期（数分〜数十分）目線のアクションを判断してください。

必ず以下の3択から1つだけ選んでください:
- "buy"  : 新規買い or 追加エントリーが有利そうな場合
- "sell" : 利確 or 損切りを含む売りが妥当そうな場合
- "hold" : まだ様子見が無難な場合

さらに、それぞれのケースで「狙いやすい価格帯」を具体的な数字で出してください。

出力フォーマットは次の JSON のみとします:

{{
  "action": "buy | sell | hold",
  "reason": "日本語で150文字以内の理由。5分足の形・板の厚さ・環境要約をどう考慮したかを具体的に。",
  "buy_zone": "買いを検討するならこの価格帯。不要なら空文字。",
  "sell_zone": "売りを検討するならこの価格帯。不要なら空文字。"
}}

※ buy_zone / sell_zone は「○○〜○○円」や「○○円付近」のように書いてください。
"""
    contents.append({"type": "input_text", "text": base_instruction})

    # 環境要約（あれば）
    if env_summary:
        contents.append(
            {
                "type": "input_text",
                "text": f"【事前に登録された日足・材料の要約】\n{env_summary}",
            }
        )

    # 5分足チャート
    contents.append(
        {
            "type": "input_text",
            "text": "▼これは5分足チャートです。直近の値動きと出来高から短期トレンドを判断してください。",
        }
    )
    contents.append({"type": "input_image", "image_url": to_data_url(chart_bytes)})

    # 板
    contents.append(
        {
            "type": "input_text",
            "text": "▼これは現在の気配値（板）の画像です。売り板・買い板の厚さや偏りを判断に使ってください。",
        }
    )
    contents.append({"type": "input_image", "image_url": to_data_url(board_bytes)})

    # メモ
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
        buy_zone = str(data.get("buy_zone", "")).strip()
        sell_zone = str(data.get("sell_zone", "")).strip()
    except Exception:
        action = "hold"
        reason = "出力解析エラーのため様子見としました。"
        buy_zone = ""
        sell_zone = ""

    if action not in ["buy", "sell", "hold"]:
        action = "hold"

    return action.upper(), reason, buy_zone, sell_zone


# ---------------- Flask ルーティング ----------------
@app.route("/", methods=["GET", "POST"])
def index():
    envs = load_all_envs()  # { code: {name, summary, updated_at}, ... }

    result = None
    error = None

    # どの銘柄が「選択中」か（環境要約表示用）
    selected_code = request.args.get("code") or ""

    if request.method == "POST":
        mode = request.form.get("mode")

        # --- ① 5分足＋板から短期アクションを判定 ---
        if mode == "judge":
            symbol_code = request.form.get("judge_symbol_code", "").strip()
            memo_text = request.form.get("judge_memo", "")

            chart_file = request.files.get("chart_image")
            board_file = request.files.get("board_image")

            if not symbol_code:
                error = "銘柄コードを入力してください。"
            elif not chart_file or not chart_file.filename:
                error = "5分足チャートの画像ファイルを選択してください。"
            elif not board_file or not board_file.filename:
                error = "気配値（板）の画像ファイルを選択してください。"
            else:
                try:
                    chart_bytes = chart_file.read()
                    board_bytes = board_file.read()

                    env_summary = None
                    symbol_name = None
                    if symbol_code in envs:
                        env_summary = envs[symbol_code].get("summary")
                        symbol_name = envs[symbol_code].get("name")

                    action, reason, buy_zone, sell_zone = analyze_intraday(
                        chart_bytes=chart_bytes,
                        board_bytes=board_bytes,
                        memo_text=memo_text,
                        env_summary=env_summary,
                        symbol_code=symbol_code,
                        symbol_name=symbol_name,
                    )
                    result = {
                        "symbol_code": symbol_code,
                        "symbol_name": symbol_name,
                        "action": action,
                        "reason": reason,
                        "buy_zone": buy_zone,
                        "sell_zone": sell_zone,
                    }
                    selected_code = symbol_code
                except Exception as e:
                    error = f"判定中にエラーが発生しました: {e}"

        # --- ② 環境情報を更新（銘柄ごとに保存） ---
        elif mode == "update_env":
            symbol_code = request.form.get("env_symbol_code", "").strip()
            symbol_name = request.form.get("env_symbol_name", "").strip()
            urls_text = request.form.get("env_urls", "")
            env_memo = request.form.get("env_memo", "")

            daily_file = request.files.get("daily_image")
            extra_files = request.files.getlist("extra_images")

            if not symbol_code:
                error = "銘柄コードは必須です。"
            else:
                daily_bytes = (
                    daily_file.read()
                    if daily_file is not None and daily_file.filename
                    else None
                )
                extra_imgs = [
                    f.read()
                    for f in extra_files
                    if f is not None and f.filename
                ]
                urls = urls_text.splitlines()

                try:
                    summary = build_environment_summary(
                        daily_bytes=daily_bytes,
                        extra_imgs=extra_imgs,
                        urls=urls,
                        memo_text=env_memo,
                        symbol_code=symbol_code,
                        symbol_name=symbol_name or symbol_code,
                    )
                    envs[symbol_code] = {
                        "name": symbol_name or symbol_code,
                        "summary": summary,
                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    save_all_envs(envs)
                    selected_code = symbol_code
                except Exception as e:
                    error = f"環境更新中にエラーが発生しました: {e}"

    # GET パラメータで銘柄指定があれば、それを優先して選択状態に
    selected_env = envs.get(selected_code) if selected_code else None

    return render_template(
        "index.html",
        result=result,
        error=error,
        envs=envs,
        selected_code=selected_code,
        selected_env=selected_env,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

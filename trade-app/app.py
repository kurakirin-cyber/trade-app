import os
import json
import base64
from flask import Flask, render_template, request, redirect, url_for

from openai import OpenAI

# ------------ 基本設定 ------------

app = Flask(__name__)

# Render の Environment Variables に設定したキーを読む
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY".lower())
client = OpenAI(api_key=OPENAI_API_KEY)

# 銘柄ごとの環境情報を保存するファイル
ENV_FILE = "stock_env.json"


# ------------ ユーティリティ ------------

def load_env_data():
    """保存済みの銘柄環境データを読み込む"""
    if not os.path.exists(ENV_FILE):
        return {}
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # 壊れていたらリセット
        return {}


def save_env_data(data):
    """銘柄環境データを書き出す"""
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def file_to_data_url(file_storage):
    """Flask の file オブジェクトを data URL (base64) に変換"""
    if not file_storage or file_storage.filename == "":
        return None

    data = file_storage.read()
    if not data:
        return None

    mime = file_storage.mimetype or "image/png"
    b64 = base64.b64encode(data).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def call_openai_for_trade(prompt_text, chart_data_url=None, board_data_url=None):
    """
    OpenAI Responses API を叩いて短期アクションを判定してもらう
    ※ type は input_text / input_image を使う
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY が設定されていません。Render の Environment Variables を確認してください。")

    content_parts = [
        {
            "type": "input_text",
            "text": prompt_text,
        }
    ]

    if chart_data_url:
        content_parts.append(
            {
                "type": "input_image",
                "image_url": {"url": chart_data_url},
            }
        )

    if board_data_url:
        content_parts.append(
            {
                "type": "input_image",
                "image_url": {"url": board_data_url},
            }
        )

    # 新しい Responses API 形式
    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {
                "role": "user",
                "content": content_parts,
            }
        ],
    )

    # 返却テキストを取り出し
    try:
        ai_text = response.output[0].content[0].text
    except Exception:
        ai_text = str(response)

    return ai_text


# ------------ ルーティング ------------

@app.route("/", methods=["GET"])
def index():
    env_data = load_env_data()
    selected_code = request.args.get("code")

    selected_env = env_data.get(selected_code) if selected_code else None

    return render_template(
        "index.html",
        env_data=env_data,
        selected_code=selected_code,
        selected_env=selected_env,
        result_text=None,
        error_message=None,
    )


@app.route("/analyze", methods=["POST"])
def analyze():
    env_data = load_env_data()

    code = (request.form.get("code") or "").strip()
    memo = (request.form.get("memo") or "").strip()

    chart_file = request.files.get("chart_image")
    board_file = request.files.get("board_image")

    chart_data_url = file_to_data_url(chart_file)
    board_data_url = file_to_data_url(board_file)

    selected_env = env_data.get(code)

    if not code:
        error_message = "銘柄コードを入力してください。"
        return render_template(
            "index.html",
            env_data=env_data,
            selected_code=None,
            selected_env=None,
            result_text=None,
            error_message=error_message,
        )

    # プロンプトを作成（環境情報があれば一緒に渡す）
    env_text = ""
    if selected_env:
        env_text = f"""
【この銘柄の環境情報】
銘柄コード: {code}
銘柄名: {selected_env.get('name', '')}
参考URL:
{selected_env.get('urls', '')}

環境メモ:
{selected_env.get('env_memo', '')}
"""

    prompt_text = f"""
あなたは日本株のデイトレード・スキャルピングをサポートするAIトレーダーです。
5分足チャート画像と板のスクショ、そしてメモをもとに、
今から「ごく短期（〜数十分）」でどうアクションするべきかを判断してください。

{env_text}

【短期トレード用メモ】
{memo}

出力は日本語で、以下の項目を必ず含めてください：

1. 現在の状況の要約（トレンド・出来高・板の雰囲気など）
2. 推奨アクション（例：様子見 / 成行で買い / 指値で買い / 利確の売り / 損切り など）
3. もし「買い」または「売り」の場合は、
   - おおよそのエントリー価格の目安（◯◯円〜◯◯円のレンジ）
   - 目標価格・利確ラインの目安
   - 損切りラインの目安
4. 判断の理由（できるだけ具体的に。チャート・板のどの部分を見たのかなど）
5. 注意点・想定外の値動きが出た時の対応案

こちらからは画像とテキストのみ渡すので、足りない情報は一般的な日本株の短期トレードの前提で補ってください。
"""

    try:
        ai_text = call_openai_for_trade(
            prompt_text=prompt_text,
            chart_data_url=chart_data_url,
            board_data_url=board_data_url,
        )
        error_message = None
    except Exception as e:
        ai_text = None
        error_message = f"判定中にエラーが発生しました: {e}"

    return render_template(
        "index.html",
        env_data=env_data,
        selected_code=code,
        selected_env=selected_env,
        result_text=ai_text,
        error_message=error_message,
    )


@app.route("/save_env", methods=["POST"])
def save_env():
    env_data = load_env_data()

    code = (request.form.get("env_code") or "").strip()
    name = (request.form.get("env_name") or "").strip()
    urls = (request.form.get("env_urls") or "").strip()
    env_memo = (request.form.get("env_memo") or "").strip()

    if not code:
        # コードは必須
        error_message = "銘柄コード（環境登録用）は必須です。"
    else:
        env_data[code] = {
            "name": name,
            "urls": urls,
            "env_memo": env_memo,
        }
        save_env_data(env_data)
        error_message = None

    selected_env = env_data.get(code) if code else None

    return render_template(
        "index.html",
        env_data=env_data,
        selected_code=code,
        selected_env=selected_env,
        result_text=None,
        error_message=error_message,
    )


# ------------ Render 用エントリーポイント ------------

if __name__ == "__main__":
    # ローカル開発用
    app.run(host="0.0.0.0", port=5000, debug=True)

import os
import json
import base64
import io
from pathlib import Path

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
)

from openai import OpenAI
from PIL import Image

# =========================================================
# 基本設定
# =========================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

ENV_JSON = DATA_DIR / "environments.json"

app = Flask(__name__)
# Render なら環境変数に入れておくのがベスト
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")

# OpenAI クライアント（Render の Environment に OPENAI_API_KEY を入れておく）
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# =========================================================
# ユーティリティ（環境データの保存 / 読み込み）
# =========================================================

def load_env_data() -> dict:
    """銘柄ごとの環境データを JSON から読み込む"""
    if not ENV_JSON.exists():
        return {}
    try:
        with ENV_JSON.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # 壊れてた場合は空で再スタート
        return {}


def save_env_data(data: dict) -> None:
    """銘柄ごとの環境データを JSON に保存"""
    with ENV_JSON.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# =========================================================
# ユーティリティ（画像 → base64 data URL）
# =========================================================

def file_to_data_url(file_storage) -> str:
    """
    Flask の FileStorage を PNG の data URL に変換
    OpenAI Responses API に input_image として渡す用
    """
    img = Image.open(file_storage.stream).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64}"


# =========================================================
# OpenAI で「5分足＋板」から短期アクション判定
# =========================================================

def judge_short_term_action(stock_code: str,
                            chart_file,
                            orderbook_file,
                            extra_note: str | None) -> str:
    """
    5分足チャート画像 + 板画像 (+ 補足メモ) をもとに
    OpenAI Responses API でアクション判定する
    """

    # 画像を data URL に変換
    chart_data_url = file_to_data_url(chart_file)
    orderbook_data_url = file_to_data_url(orderbook_file)

    # その銘柄の環境データを読み込み（あれば）
    env_data = load_env_data()
    env = env_data.get(stock_code, {})

    env_text_parts = []
    if env.get("stock_name"):
        env_text_parts.append(f"銘柄名: {env['stock_name']}")
    if env.get("urls"):
        env_text_parts.append(f"参考URL:\n{env['urls']}")
    if env.get("memo"):
        env_text_parts.append(f"環境メモ:\n{env['memo']}")

    env_text = "\n\n".join(env_text_parts) if env_text_parts else "登録された環境情報はありません。"

    prompt = f"""
あなたは日本株のデイトレードを支援するアナリストです。

銘柄コード: {stock_code}

この銘柄について、以下の情報を考慮して
「ごく短期（数時間〜当日中）のトレードアクション」を判定してください。

【入力情報】
- 5分足チャート画像
- 板（気配値）の画像
- 補足メモ
- 事前に登録されている日足・材料などの環境メモ

【登録済みの環境情報】
{env_text}

【補足メモ】（ユーザー入力）
{extra_note or "（特に補足なし）"}

【出力フォーマット（必ずこの形式で）】

1. 今の短期アクション
   - 「今すぐ買い」「押し目待ち」「今すぐ売り」「ホールド」のいずれかを1つだけ。

2. アクションの理由（5〜7行程度）
   - 5分足のトレンド、出来高の勢い、板の厚さ・偏りなどを根拠に説明。

3. もし「ホールド」の場合：
   - 追加で買うなら狙いたい価格帯（○○円〜○○円）

4. もし「今すぐ売り」の場合：
   - 利確・損切りの目安価格帯（○○円〜○○円）

5. もし「今すぐ買い」の場合：
   - エントリーしたい価格帯（○○円〜○○円）

日本語で、箇条書きを使ってわかりやすく出力してください。
"""

    # ★ここが肝心：Responses API に合わせて type を input_text / input_image にする
    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": prompt.strip(),
                    },
                    {
                        "type": "input_image",
                        "image_url": {"url": chart_data_url},
                    },
                    {
                        "type": "input_image",
                        "image_url": {"url": orderbook_data_url},
                    },
                ],
            }
        ],
        max_output_tokens=800,
    )

    # 出力テキストを取り出す
    result_text = ""
    for out in response.output:
        for block in out.content:
            if getattr(block, "type", "") == "output_text":
                result_text += block.text

    return result_text.strip()


# =========================================================
# ルーティング
# =========================================================

@app.route("/", methods=["GET"])
def index():
    """
    メイン画面表示
    - 登録済み銘柄一覧
    - 5分足＋板判定フォーム
    - 環境登録フォーム
    """
    env_data = load_env_data()
    # 上部の「登録済み銘柄一覧」に出す用
    registered_envs = [
        {
            "code": code,
            "name": info.get("stock_name", ""),
            "memo": info.get("memo", ""),
            "urls": info.get("urls", ""),
        }
        for code, info in env_data.items()
    ]
    # index.html 側で {{ registered_envs }} を使って一覧表示してね
    return render_template(
        "index.html",
        registered_envs=registered_envs,
        judge_result=None,
    )


@app.route("/judge", methods=["POST"])
def handle_judge():
    """
    ① 5分足＋板から短期アクションを判定
    フォームの name は以下を想定：
      stock_code        … 判定対象の銘柄コード
      chart_image       … 5分足チャート画像
      orderbook_image   … 板画像
      extra_note        … 補足メモ（任意）
    """
    stock_code = request.form.get("stock_code", "").strip()
    chart_file = request.files.get("chart_image")
    orderbook_file = request.files.get("orderbook_image")
    extra_note = request.form.get("extra_note", "").strip()

    if not stock_code:
        flash("銘柄コードを入力してください。", "error")
        return redirect(url_for("index"))

    if not chart_file or not orderbook_file:
        flash("5分足チャート画像と板画像は必須です。", "error")
        return redirect(url_for("index"))

    try:
        result_text = judge_short_term_action(
            stock_code=stock_code,
            chart_file=chart_file,
            orderbook_file=orderbook_file,
            extra_note=extra_note,
        )
    except Exception as e:
        flash(f"判定中にエラーが発生しました: {e}", "error")
        return redirect(url_for("index"))

    env_data = load_env_data()
    registered_envs = [
        {
            "code": code,
            "name": info.get("stock_name", ""),
            "memo": info.get("memo", ""),
            "urls": info.get("urls", ""),
        }
        for code, info in env_data.items()
    ]

    # 判定結果を同じ画面の下部に表示する想定
    return render_template(
        "index.html",
        registered_envs=registered_envs,
        judge_result=result_text,
        judged_code=stock_code,
    )


@app.route("/save_environment", methods=["POST"])
def save_environment():
    """
    ② 日足・材料などの環境情報を登録（銘柄ごとに保存）

    フォームの name は以下を想定：
      env_stock_code … 銘柄コード（必須）
      env_stock_name … 銘柄名（任意・メモ代わり）
      env_urls       … 参考URL（複数行OK）
      env_memo       … 環境メモ（任意）
    画像（日足など）は一旦保存せず、テキストだけを AI に渡す想定。
    """
    stock_code = request.form.get("env_stock_code", "").strip()
    stock_name = request.form.get("env_stock_name", "").strip()
    urls = request.form.get("env_urls", "").strip()
    memo = request.form.get("env_memo", "").strip()

    if not stock_code:
        flash("環境登録用の銘柄コードは必須です。", "error")
        return redirect(url_for("index"))

    data = load_env_data()
    data[stock_code] = {
        "stock_name": stock_name,
        "urls": urls,
        "memo": memo,
    }
    save_env_data(data)

    flash(f"銘柄 {stock_code} の環境情報を保存しました。", "success")
    return redirect(url_for("index"))


# =========================================================
# エントリポイント（ローカル実行用）
# =========================================================

if __name__ == "__main__":
    # Render 上では使われないが、ローカルデバッグ用
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)

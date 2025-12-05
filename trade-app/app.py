import os
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for, flash
import google.generativeai as genai
from dotenv import load_dotenv
import PIL.Image

# ローカル開発用の設定読み込み
load_dotenv()

app = Flask(__name__)
app.secret_key = 'super_secret_key_for_flash_messages'  # Flashメッセージに必要

# Geminiの設定 (Renderの環境変数から取得)
GENAI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GENAI_API_KEY)

# モデル設定 (Proの方が文章の読解力が高いからおすすめやけど、遅いならFlashにしてな)
model = genai.GenerativeModel('gemini-1.5-pro-latest')

# 簡易データベース (メモリ上に保存。再起動で消えるから注意な！)
# 構造: { "9984": { "name": "SBG", "memo": "...", "urls": "...", "scraped_text": "..." } }
STOCKS_DB = {}

def fetch_url_content(url_text):
    """URLからテキスト情報をざっくり引っこ抜く関数"""
    if not url_text:
        return ""
    
    urls = [u.strip() for u in url_text.split('\n') if u.strip().startswith('http')]
    combined_text = ""
    
    for url in urls:
        try:
            # ニュースサイトとかの情報を取得
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.content, 'html.parser')
                # 本文っぽいやつだけ抽出 (pタグとか)
                text = ' '.join([p.text for p in soup.find_all(['p', 'h1', 'h2'])])
                combined_text += f"\n[URL情報: {url}]\n{text[:1000]}..." # 長すぎるとアレやから1000文字で切る
        except Exception as e:
            print(f"URL読み込み失敗: {e}")
    
    return combined_text

@app.route('/')
def index():
    # トップページ表示。保存してる銘柄情報を渡す
    return render_template('index.html', registered_envs=STOCKS_DB)

@app.route('/save_environment', methods=['POST'])
def save_environment():
    """環境認識情報を保存する"""
    code = request.form.get('env_stock_code')
    name = request.form.get('env_stock_name')
    urls = request.form.get('env_urls')
    memo = request.form.get('env_memo')

    if not code:
        flash('銘柄コードがないと保存できへんで！', 'error')
        return redirect(url_for('index'))

    # URLの中身をスクレイピングして要約用テキストを作る
    scraped_text = fetch_url_content(urls)

    # メモリに保存
    STOCKS_DB[code] = {
        "name": name,
        "urls": urls,
        "memo": memo,
        "scraped_text": scraped_text  # AIに読ませる用
    }

    flash(f'銘柄コード {code} ({name}) の環境情報を保存したで！', 'success')
    return redirect(url_for('index'))

@app.route('/judge', methods=['POST'])
def judge():
    """5分足と板画像＋保存情報の統合判断"""
    try:
        if not GENAI_API_KEY:
            flash('APIキーが設定されてへんで！Renderの設定見てな。', 'error')
            return redirect(url_for('index'))

        # フォームからの入力
        code = request.form.get('stock_code')
        extra_note = request.form.get('extra_note')
        chart_file = request.files.get('chart_image')
        board_file = request.files.get('orderbook_image')

        # 画像チェック
        if not chart_file or not board_file:
            flash('画像は2枚とも必須やで！', 'error')
            return redirect(url_for('index'))

        # 保存されている環境情報を取得
        env_info = STOCKS_DB.get(code, {})
        env_context = f"""
        [事前登録された環境認識情報]
        銘柄名: {env_info.get('name', '不明')}
        メモ: {env_info.get('memo', 'なし')}
        ニュース/資料の要約情報: {env_info.get('scraped_text', 'なし')}
        """

        # 画像を開く
        chart_img = PIL.Image.open(chart_file)
        board_img = PIL.Image.open(board_file)

        # プロンプト作成
        prompt = f"""
        あなたは超一流のデイトレーダーです。
        以下の情報に基づき、**HTML形式**で見やすく判断を出力してください。

        【入力情報】
        1. 5分足チャート画像（今のトレンドとエントリータイミング用）
        2. 板情報画像（需給の強さ用）
        3. 補足メモ: {extra_note}
        4. 背景情報（日足・材料など）:
        {env_context}

        【出力要件】
        JSONではなく、Webページに埋め込むための**HTMLタグ**を使って出力してください。
        以下の構成でお願いします。
        - 結論（<h3>タグで、色付き文字などで BUY / SELL / WAIT を強調）
        - エントリー・利確・損切りの具体的数値（<ul>リスト形式）
        - 根拠の解説（<p>タグ。板の厚さ、チャートの形、背景情報を絡めて論理的に）
        - リスク注意点（あれば）

        結論はズバッと言ってください。
        """

        # Geminiに投げる
        response = model.generate_content([prompt, chart_img, board_img])
        result_html = response.text

        # 結果を表示するためにページに戻る
        # 入力値を保持するために form_values も渡す
        return render_template('index.html', 
                             judge_result=result_html,
                             registered_envs=STOCKS_DB,
                             form_values={'stock_code': code, 'extra_note': extra_note})

    except Exception as e:
        print(f"Error: {e}")
        flash(f'エラー起きたわ...: {str(e)}', 'error')
        return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)

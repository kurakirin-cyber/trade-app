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
app.secret_key = 'super_secret_key_for_flash_messages'

# Geminiの設定 (Renderの環境変数から取得)
GENAI_API_KEY = os.getenv("GEMINI_API_KEY")
if GENAI_API_KEY:
    genai.configure(api_key=GENAI_API_KEY)

# モデル設定 (最新の2.5-proを指定！)
# ここがエラーの原因やった場所や。確実に存在する名前に変更したで。
model = genai.GenerativeModel('gemini-2.5-pro')

# 簡易データベース
STOCKS_DB = {}

def fetch_url_content(url_text):
    """URLからテキスト情報をざっくり引っこ抜く関数"""
    if not url_text:
        return ""
    
    urls = [u.strip() for u in url_text.split('\n') if u.strip().startswith('http')]
    combined_text = ""
    
    for url in urls:
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.content, 'html.parser')
                text = ' '.join([p.text for p in soup.find_all(['p', 'h1', 'h2'])])
                clean_text = " ".join(text.split())
                combined_text += f"\n[URL情報: {url}]\n{clean_text[:800]}..." 
        except Exception as e:
            print(f"URL読み込み失敗: {e}")
            combined_text += f"\n[URL読込エラー: {url}]"
    
    return combined_text

@app.route('/')
def index():
    return render_template('index.html', registered_envs=STOCKS_DB)

@app.route('/save_environment', methods=['POST'])
def save_environment():
    code = request.form.get('env_stock_code')
    name = request.form.get('env_stock_name')
    urls = request.form.get('env_urls')
    memo = request.form.get('env_memo')

    if not code:
        flash('銘柄コードがないと保存できへんで！', 'error')
        return redirect(url_for('index'))

    scraped_text = fetch_url_content(urls) if urls else ""

    STOCKS_DB[code] = {
        "name": name if name else code,
        "memo": memo,
        "scraped_text": scraped_text
    }

    flash(f'銘柄コード {code} の環境情報を保存したで！', 'success')
    return redirect(url_for('index'))

@app.route('/judge', methods=['GET', 'POST'])
def judge():
    # 手ぶら(GET)で来たらトップへ戻す
    if request.method == 'GET':
        return redirect(url_for('index'))

    try:
        if not GENAI_API_KEY:
            flash('APIキーが設定されてへんで！Renderの設定画面で GEMINI_API_KEY を入れてな。', 'error')
            return redirect(url_for('index'))

        code = request.form.get('stock_code')
        extra_note = request.form.get('extra_note')
        chart_file = request.files.get('chart_image')
        board_file = request.files.get('orderbook_image')

        if not chart_file or not board_file:
            flash('画像は2枚とも必須やで！', 'error')
            return redirect(url_for('index'))

        env_info = STOCKS_DB.get(code, {})
        env_context = f"""
        [事前登録された環境認識情報]
        銘柄名: {env_info.get('name', '不明')}
        メモ: {env_info.get('memo', 'なし')}
        ニュース/資料の要約情報: {env_info.get('scraped_text', 'なし')}
        """

        chart_img = PIL.Image.open(chart_file)
        board_img = PIL.Image.open(board_file)

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
        JSONではなく、Webページに埋め込むための**HTMLタグ**のみを出力してください（```html 不要）。
        以下の構成でお願いします。
        - <div class="p-4 bg-gray-50 rounded-lg"> で囲むこと。
        - 結論（<h3>タグで、class="text-2xl font-bold mb-2" を付与。 BUY / SELL / WAIT を色付きで強調）
        - エントリー・利確・損切りの具体的数値（<ul>リスト形式）
        - 根拠の解説（<p>タグ。板の厚さ、チャートの形、背景情報を絡めて論理的に）
        - リスク注意点（あれば）
        
        結論はズバッと言ってください。関西弁で出力してください。
        """

        response = model.generate_content([prompt, chart_img, board_img])
        result_html = response.text
        
        # 不要なMarkdown記号を削除
        result_html = result_html.replace('```html', '').replace('```', '')

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

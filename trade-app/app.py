import os
import requests
import base64
import io
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import google.generativeai as genai
from dotenv import load_dotenv
import PIL.Image

load_dotenv()

app = Flask(__name__)
app.secret_key = 'super_secret_key_for_flash_messages'

# Geminiの設定
GENAI_API_KEY = os.getenv("GEMINI_API_KEY")
if GENAI_API_KEY:
    genai.configure(api_key=GENAI_API_KEY)

# モデル設定 (爆速のFlashを使用)
model = genai.GenerativeModel('gemini-2.5-flash')

# データベース (メモリ上)
# 構造: { "9984": { "name": "...", "memo": "...", "news_text": "...", "financial_text": "...", "daily_chart_b64": "..." } }
STOCKS_DB = {}

def image_to_base64(img):
    """画像をBase64文字列に変換してメモリ節約"""
    # サーバー圧迫を防ぐため、最大サイズを縮小
    img.thumbnail((1024, 1024)) 
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG")
    return base64.b64encode(buffered.getvalue()).decode('utf-8')

def base64_to_image(b64_str):
    """Base64文字列をPIL画像に戻す"""
    return PIL.Image.open(io.BytesIO(base64.b64decode(b64_str)))

def fetch_url_content(url_text):
    """URLからテキスト情報を引っこ抜く"""
    if not url_text: return ""
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
                combined_text += f"\n[URL: {url}] {clean_text[:500]}..." 
        except Exception as e:
            combined_text += f"\n[エラー: {url}]"
    return combined_text

def summarize_financial_image(image_file):
    """決算書画像をAIに読ませて要約テキストにする"""
    try:
        img = PIL.Image.open(image_file)
        prompt = "この決算資料（または適時開示）の画像から、デイトレード判断に重要そうな「業績の修正」「サプライズ要素」「ポジティブ/ネガティブな数字」を300文字以内で要約してください。"
        response = model.generate_content([prompt, img])
        return response.text
    except Exception as e:
        return f"決算書読み込みエラー: {e}"

@app.route('/')
def index():
    return render_template('index.html', registered_envs=STOCKS_DB)

@app.route('/get_stock/<code_id>')
def get_stock(code_id):
    """選択された銘柄の情報を返すAPI"""
    data = STOCKS_DB.get(code_id)
    if data:
        # 画像データは重いのでJSONには含めず、有無だけ返す
        response_data = data.copy()
        response_data['has_daily_chart'] = bool(data.get('daily_chart_b64'))
        if 'daily_chart_b64' in response_data:
            del response_data['daily_chart_b64']
        return jsonify(response_data)
    return jsonify({}), 404

@app.route('/register_stock', methods=['POST'])
def register_stock():
    """銘柄情報の登録・更新"""
    try:
        code = request.form.get('reg_code')
        name = request.form.get('reg_name')
        
        if not code:
            flash('銘柄コードは必須やで！', 'error')
            return redirect(url_for('index'))

        # 既存データの取得（なければ新規作成）
        current_data = STOCKS_DB.get(code, {
            "name": name, 
            "memo": "", 
            "news_text": "", 
            "financial_text": "",
            "daily_chart_b64": None
        })
        
        # 名前更新
        if name: current_data['name'] = name

        # 1. 日足チャート (アップロードされたら即更新)
        daily_chart_file = request.files.get('reg_daily_chart')
        if daily_chart_file and daily_chart_file.filename != '':
            img = PIL.Image.open(daily_chart_file)
            current_data['daily_chart_b64'] = image_to_base64(img)

        # 2. ニュースURL (追加 or 更新)
        url_mode = request.form.get('news_mode', 'append') # append or overwrite
        new_urls = request.form.get('reg_urls')
        if new_urls:
            scraped_text = fetch_url_content(new_urls)
            if url_mode == 'overwrite':
                current_data['news_text'] = scraped_text
            else:
                current_data['news_text'] += "\n" + scraped_text

        # 3. 決算書画像 (追加 or 更新 -> AI要約してテキスト保存)
        financial_mode = request.form.get('financial_mode', 'append')
        financial_file = request.files.get('reg_financial_file')
        if financial_file and financial_file.filename != '':
            summary = summarize_financial_image(financial_file)
            if financial_mode == 'overwrite':
                current_data['financial_text'] = summary
            else:
                current_data['financial_text'] += "\n[追加情報] " + summary

        # 4. メモ
        new_memo = request.form.get('reg_memo')
        if new_memo:
            current_data['memo'] = new_memo

        # DB保存
        STOCKS_DB[code] = current_data
        flash(f'銘柄 {code} ({current_data["name"]}) の情報を更新したで！', 'success')
        
    except Exception as e:
        print(e)
        flash(f'登録エラー: {e}', 'error')

    return redirect(url_for('index'))

@app.route('/judge', methods=['GET', 'POST'])
def judge():
    if request.method == 'GET': return redirect(url_for('index'))

    try:
        if not GENAI_API_KEY:
            flash('APIキー設定してな！', 'error')
            return redirect(url_for('index'))

        code = request.form.get('stock_code')
        extra_note = request.form.get('extra_note')
        chart_file = request.files.get('chart_image') # 5分足
        board_file = request.files.get('orderbook_image') # 板

        if not chart_file or not board_file:
            flash('5分足と板画像は必須やで！', 'error')
            return redirect(url_for('index'))

        # DBから環境情報を取得
        env_data = STOCKS_DB.get(code, {})
        env_text = f"""
        [事前登録情報]
        銘柄名: {env_data.get('name', '不明')}
        メモ: {env_data.get('memo', 'なし')}
        ニュース/URL情報: {env_data.get('news_text', 'なし')}
        決算/材料の要約: {env_data.get('financial_text', 'なし')}
        """

        # 画像リストを作成（Geminiに渡す用）
        images_to_pass = []
        
        # 1. 5分足 (必須)
        img_5min = PIL.Image.open(chart_file)
        images_to_pass.append(img_5min)
        
        # 2. 板画像 (必須)
        img_board = PIL.Image.open(board_file)
        images_to_pass.append(img_board)

        # 3. 日足画像 (DBにあれば追加)
        daily_chart_b64 = env_data.get('daily_chart_b64')
        daily_chart_status = "なし"
        if daily_chart_b64:
            img_daily = base64_to_image(daily_chart_b64)
            images_to_pass.append(img_daily)
            daily_chart_status = "あり（画像3枚目）"

        prompt = f"""
        あなたはデイトレーダーです。以下の情報を統合し**HTML**で判断を出力してください。
        
        【入力画像】
        1枚目: 5分足チャート（短期トレンド）
        2枚目: 板情報（需給）
        3枚目: 日足チャート（中長期トレンド・環境認識） ※もしあれば

        【テキスト情報】
        補足メモ: {extra_note}
        日足画像の有無: {daily_chart_status}
        {env_text}

        【指示】
        - 日足（3枚目）がある場合は、日足の抵抗線やトレンドを考慮して、5分足のエントリー根拠を補強してください。
        - 決算情報の要約がある場合は、ファンダメンタルズ要素として加味してください。
        - 出力は <div class="p-4 bg-gray-50 rounded-lg"> で囲み、<h3>で結論(BUY/SELL/WAIT)、<ul>で数値、<p>で根拠を記述。
        - 関西弁で。
        """

        response = model.generate_content([prompt] + images_to_pass)
        result_html = response.text.replace('```html', '').replace('```', '')

        return render_template('index.html', 
                             judge_result=result_html,
                             registered_envs=STOCKS_DB,
                             form_values={'stock_code': code, 'extra_note': extra_note})

    except Exception as e:
        flash(f'エラー: {str(e)}', 'error')
        return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)

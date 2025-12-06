import os
import requests
import base64
import io
import json
import mimetypes
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

# モデル設定 (PDFも扱えるFlashを使用)
model = genai.GenerativeModel('gemini-2.5-flash')

# データ保存用ファイル名
DB_FILE = 'stock_data.json'

def load_db():
    """JSONファイルからデータを読み込む"""
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"DB読み込みエラー: {e}")
            return {}
    return {}

def save_db(data):
    """データをJSONファイルに保存する"""
    try:
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"DB保存エラー: {e}")

def image_to_base64(img):
    """画像をBase64文字列に変換"""
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

def summarize_financial_file(file_storage):
    """決算書(画像orPDF)をAIに読ませて要約テキストにする"""
    try:
        filename = file_storage.filename
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        file_data = file_storage.read()

        # PDFか画像かで処理を分けるが、Gemini APIにはmime_type指定でバイト列を渡せる
        parts = [
            {"mime_type": mime_type, "data": file_data},
            "この決算資料（または適時開示）から、デイトレード判断に重要そうな「業績の修正」「サプライズ要素」「ポジティブ/ネガティブな数字」を300文字以内で要約してください。"
        ]
        
        response = model.generate_content(parts)
        return response.text
    except Exception as e:
        return f"決算書読み込みエラー: {e}"

@app.route('/')
def index():
    db = load_db()
    return render_template('index.html', registered_envs=db)

@app.route('/get_stock/<code_id>')
def get_stock(code_id):
    """選択された銘柄の情報を返すAPI"""
    db = load_db()
    data = db.get(code_id)
    if data:
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
        db = load_db()
        
        code = request.form.get('reg_code')
        name = request.form.get('reg_name')
        
        # 新機能: 保有株・取得単価
        holding_qty = request.form.get('reg_holding_qty', '0')
        avg_cost = request.form.get('reg_avg_cost', '0')

        if not code:
            flash('銘柄コードは必須やで！', 'error')
            return redirect(url_for('index'))

        # 既存データの取得
        current_data = db.get(code, {
            "name": name, 
            "memo": "", 
            "news_text": "", 
            "financial_text": "",
            "daily_chart_b64": None,
            "holding_qty": "0",
            "avg_cost": "0"
        })
        
        # 基本情報更新
        if name: current_data['name'] = name
        current_data['holding_qty'] = holding_qty
        current_data['avg_cost'] = avg_cost

        # 1. 日足チャート
        daily_chart_file = request.files.get('reg_daily_chart')
        if daily_chart_file and daily_chart_file.filename != '':
            img = PIL.Image.open(daily_chart_file)
            current_data['daily_chart_b64'] = image_to_base64(img)

        # 2. ニュースURL
        url_mode = request.form.get('news_mode', 'append')
        new_urls = request.form.get('reg_urls')
        if new_urls:
            scraped_text = fetch_url_content(new_urls)
            if url_mode == 'overwrite':
                current_data['news_text'] = scraped_text
            else:
                current_data['news_text'] += "\n" + scraped_text

        # 3. 決算書 (画像 or PDF)
        financial_mode = request.form.get('financial_mode', 'append')
        financial_file = request.files.get('reg_financial_file')
        if financial_file and financial_file.filename != '':
            summary = summarize_financial_file(financial_file)
            if financial_mode == 'overwrite':
                current_data['financial_text'] = summary
            else:
                current_data['financial_text'] += "\n[追加情報] " + summary

        # 4. メモ
        new_memo = request.form.get('reg_memo')
        if new_memo:
            current_data['memo'] = new_memo

        # 保存
        db[code] = current_data
        save_db(db)
        
        flash(f'銘柄 {code} ({current_data["name"]}) の情報を保存したで！', 'success')
        
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
        chart_file = request.files.get('chart_image') 
        board_file = request.files.get('orderbook_image')

        if not chart_file or not board_file:
            flash('5分足と板画像は必須やで！', 'error')
            return redirect(url_for('index'))

        # DBから情報取得
        db = load_db()
        env_data = db.get(code, {})
        
        # 保有状況の取得
        qty = env_data.get('holding_qty', '0')
        cost = env_data.get('avg_cost', '0')
        holding_status = f"【現在の保有状況】保有数: {qty}株 / 平均取得単価: {cost}円"

        env_text = f"""
        [事前登録情報]
        銘柄名: {env_data.get('name', '不明')}
        {holding_status}
        メモ: {env_data.get('memo', 'なし')}
        ニュース/URL情報: {env_data.get('news_text', 'なし')}
        決算/材料の要約: {env_data.get('financial_text', 'なし')}
        """

        # 画像リスト
        images_to_pass = []
        img_5min = PIL.Image.open(chart_file)
        images_to_pass.append(img_5min)
        
        img_board = PIL.Image.open(board_file)
        images_to_pass.append(img_board)

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
        3枚目: 日足チャート（環境認識） ※もしあれば

        【テキスト情報】
        補足メモ: {extra_note}
        日足画像の有無: {daily_chart_status}
        {env_text}

        【指示】
        - ユーザーは現在 **「{qty}株」を「{cost}円」** で保有しています。この取得単価と比較して、現在は含み益か含み損かを考慮し、「ナンピンすべきか」「損切りすべきか」「利確すべきか」「買い増すべきか」を明確にアドバイスしてください。
        - 決算情報や日足を考慮し、5分足のエントリー根拠を補強してください。
        - 出力は <div class="p-4 bg-gray-50 rounded-lg"> で囲み、<h3>で結論(BUY/SELL/WAIT)、<ul>で数値目標、<p>で根拠を記述。
        - 関西弁で。
        """

        response = model.generate_content([prompt] + images_to_pass)
        result_html = response.text.replace('```html', '').replace('```', '')

        return render_template('index.html', 
                             judge_result=result_html,
                             registered_envs=db,
                             form_values={'stock_code': code, 'extra_note': extra_note})

    except Exception as e:
        flash(f'エラー: {str(e)}', 'error')
        return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)

import os
import requests
import base64
import io
import mimetypes
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import google.generativeai as genai
from dotenv import load_dotenv
import PIL.Image
from pymongo import MongoClient

load_dotenv()

app = Flask(__name__)
app.secret_key = 'super_secret_key_for_flash_messages'

# Geminiの設定
GENAI_API_KEY = os.getenv("GEMINI_API_KEY")
if GENAI_API_KEY:
    genai.configure(api_key=GENAI_API_KEY)

# モデル設定
model = genai.GenerativeModel('gemini-2.5-flash')

# --- MongoDBの設定 ---
MONGO_URI = os.getenv("MONGO_URI")

# DB接続関数 (接続エラーが起きてもアプリ自体は落ちないように)
def get_db_collection():
    if not MONGO_URI:
        print("【警告】MONGO_URIが設定されてへんで！データ保存できへんよ！")
        return None
    try:
        # 接続開始
        client = MongoClient(MONGO_URI)
        db = client['stock_app_db']  # データベース名（自由）
        collection = db['stocks']    # コレクション名（自由）
        return collection
    except Exception as e:
        print(f"MongoDB接続エラー: {e}")
        return None

# --- 画像処理系 ---
def image_to_base64(img):
    img.thumbnail((1024, 1024)) 
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG")
    return base64.b64encode(buffered.getvalue()).decode('utf-8')

def base64_to_image(b64_str):
    return PIL.Image.open(io.BytesIO(base64.b64decode(b64_str)))

def fetch_url_content(url_text):
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
    try:
        filename = file_storage.filename
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        file_data = file_storage.read()
        parts = [
            {"mime_type": mime_type, "data": file_data},
            "この決算資料（または適時開示）から、デイトレード判断に重要そうな「業績の修正」「サプライズ要素」「ポジティブ/ネガティブな数字」を300文字以内で要約してください。"
        ]
        response = model.generate_content(parts)
        return response.text
    except Exception as e:
        return f"決算書読み込みエラー: {e}"

# --- ルート設定 ---

@app.route('/')
def index():
    # MongoDBから全データを取得して辞書形式に変換
    stocks_data = {}
    collection = get_db_collection()
    
    # 【修正箇所】if collection: を if collection is not None: に変更
    if collection is not None:
        # 全件取得
        cursor = collection.find({})
        for doc in cursor:
            code = doc.get('code')
            if code:
                stocks_data[code] = doc
    
    return render_template('index.html', registered_envs=stocks_data)

@app.route('/get_stock/<code_id>')
def get_stock(code_id):
    """API: 選択された銘柄情報を返す"""
    collection = get_db_collection()
    # 【修正箇所】if not collection: を if collection is None: に変更
    if collection is None:
        return jsonify({}), 500

    data = collection.find_one({"code": code_id})
    if data:
        # ObjectIdなどJSON化できないものを除外するためにコピー作成
        response_data = {k: v for k, v in data.items() if k != '_id'}
        
        # 画像データは重いので有無フラグだけ返す
        response_data['has_daily_chart'] = bool(response_data.get('daily_chart_b64'))
        if 'daily_chart_b64' in response_data:
            del response_data['daily_chart_b64']
            
        return jsonify(response_data)
    
    return jsonify({}), 404

@app.route('/register_stock', methods=['POST'])
def register_stock():
    """銘柄情報の登録・更新 (MongoDB版)"""
    try:
        collection = get_db_collection()
        # 【修正箇所】if not collection: を if collection is None: に変更
        if collection is None:
            flash('データベースに接続できへんかった...設定確認してな', 'error')
            return redirect(url_for('index'))
            
        code = request.form.get('reg_code')
        name = request.form.get('reg_name')
        
        # 保有情報
        holding_qty = request.form.get('reg_holding_qty', '0')
        avg_cost = request.form.get('reg_avg_cost', '0')

        if not code:
            flash('銘柄コードは必須やで！', 'error')
            return redirect(url_for('index'))

        # DBから既存データを取得、なければ初期値
        existing_data = collection.find_one({"code": code}) or {}
        
        # 更新用データを作成 (既存データをベースに)
        update_data = {
            "code": code, # キーとして保存
            "name": name if name else existing_data.get('name', ''),
            "memo": existing_data.get('memo', ''),
            "news_text": existing_data.get('news_text', ''),
            "financial_text": existing_data.get('financial_text', ''),
            "daily_chart_b64": existing_data.get('daily_chart_b64', None),
            "holding_qty": holding_qty,
            "avg_cost": avg_cost
        }

        # 1. 日足チャート
        daily_chart_file = request.files.get('reg_daily_chart')
        if daily_chart_file and daily_chart_file.filename != '':
            img = PIL.Image.open(daily_chart_file)
            update_data['daily_chart_b64'] = image_to_base64(img)

        # 2. ニュースURL
        url_mode = request.form.get('news_mode', 'append')
        new_urls = request.form.get('reg_urls')
        if new_urls:
            scraped_text = fetch_url_content(new_urls)
            if url_mode == 'overwrite':
                update_data['news_text'] = scraped_text
            else:
                current = update_data['news_text']
                update_data['news_text'] = (current + "\n" + scraped_text) if current else scraped_text

        # 3. 決算書
        financial_mode = request.form.get('financial_mode', 'append')
        financial_file = request.files.get('reg_financial_file')
        if financial_file and financial_file.filename != '':
            summary = summarize_financial_file(financial_file)
            if financial_mode == 'overwrite':
                update_data['financial_text'] = summary
            else:
                current = update_data['financial_text']
                update_data['financial_text'] = (current + "\n[追加情報] " + summary) if current else summary

        # 4. メモ
        new_memo = request.form.get('reg_memo')
        if new_memo:
            update_data['memo'] = new_memo

        # MongoDBに保存 (なければ挿入、あれば更新: upsert=True)
        collection.update_one(
            {"code": code},
            {"$set": update_data},
            upsert=True
        )
        
        flash(f'銘柄 {code} ({update_data["name"]}) の情報をクラウドDBに保存したで！', 'success')
        
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
        collection = get_db_collection()
        env_data = {}
        # 【修正箇所】if collection: を if collection is not None: に変更
        if collection is not None:
            env_data = collection.find_one({"code": code}) or {}
        
        # 保有状況
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
        
        # テンプレートに渡すデータを用意
        stocks_data = {}
        # 【修正箇所】if collection: を if collection is not None: に変更
        if collection is not None:
            cursor = collection.find({})
            for doc in cursor:
                c = doc.get('code')
                if c: stocks_data[c] = doc

        return render_template('index.html', 
                             judge_result=result_html,
                             registered_envs=stocks_data,
                             form_values={'stock_code': code, 'extra_note': extra_note})

    except Exception as e:
        flash(f'エラー: {str(e)}', 'error')
        return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)

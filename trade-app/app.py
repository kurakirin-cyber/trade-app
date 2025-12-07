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
model = genai.GenerativeModel('gemini-2.0-flash-exp')

# --- MongoDBの設定 ---
MONGO_URI = os.getenv("MONGO_URI")

def get_db_collection():
    if not MONGO_URI:
        print("【警告】MONGO_URIが設定されてへんで！")
        return None
    try:
        client = MongoClient(MONGO_URI)
        db = client['stock_app_db']
        collection = db['stocks']
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
    stocks_data = {}
    collection = get_db_collection()
    
    # 修正箇所: is not None を追加
    if collection is not None:
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
    # 修正箇所: is None を追加
    if collection is None:
        return jsonify({}), 500

    data = collection.find_one({"code": code_id})
    if data:
        response_data = {k: v for k, v in data.items() if k != '_id'}
        
        response_data['has_daily_chart'] = bool(response_data.get('daily_chart_b64'))
        if 'daily_chart_b64' in response_data:
            del response_data['daily_chart_b64']
        
        response_data['has_financial_info'] = bool(response_data.get('financial_text'))
            
        return jsonify(response_data)
    
    return jsonify({}), 404

@app.route('/register_stock', methods=['POST'])
def register_stock():
    """銘柄情報の登録・更新"""
    try:
        collection = get_db_collection()
        # 修正箇所: is None を追加
        if collection is None:
            flash('DB接続エラー', 'error')
            return redirect(url_for('index'))
            
        code = request.form.get('reg_code')
        name = request.form.get('reg_name')
        
        if not code:
            flash('銘柄コードは必須やで！', 'error')
            return redirect(url_for('index'))

        existing_data = collection.find_one({"code": code}) or {}
        
        update_data = {
            "code": code,
            "name": name if name else existing_data.get('name', ''),
            "memo": existing_data.get('memo', ''),
            "news_text": existing_data.get('news_text', ''),
            "saved_urls": existing_data.get('saved_urls', ''),
            "financial_text": existing_data.get('financial_text', ''),
            "daily_chart_b64": existing_data.get('daily_chart_b64', None),
            "holding_qty": request.form.get('reg_holding_qty', '0'),
            "avg_cost": request.form.get('reg_avg_cost', '0')
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
                update_data['saved_urls'] = new_urls
            else:
                current_news = update_data['news_text']
                current_urls = update_data['saved_urls']
                update_data['news_text'] = (current_news + "\n" + scraped_text) if current_news else scraped_text
                update_data['saved_urls'] = (current_urls + "\n" + new_urls) if current_urls else new_urls

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

        collection.update_one({"code": code}, {"$set": update_data}, upsert=True)
        flash(f'銘柄 {code} を保存したで！', 'success')
        
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

        collection = get_db_collection()
        env_data = {}
        # 修正箇所: is not None を追加
        if collection is not None:
            env_data = collection.find_one({"code": code}) or {}
        
        qty = env_data.get('holding_qty', '0')
        cost = env_data.get('avg_cost', '0')
        
        daily_chart_b64 = env_data.get('daily_chart_b64')
        images_to_pass = [PIL.Image.open(chart_file), PIL.Image.open(board_file)]
        daily_status = "なし"
        
        if daily_chart_b64:
            images_to_pass.append(base64_to_image(daily_chart_b64))
            daily_status = "あり（画像3枚目）"

        prompt = f"""
        あなたはプロのデイトレーダーです。以下の情報を統合し、現在の局面における最適な売買判断を下してください。
        
        【ユーザーの保有状況】
        保有数: {qty}株
        平均取得単価: {cost}円
        
        【環境認識データ】
        銘柄名: {env_data.get('name', '不明')} ({code})
        事前メモ: {env_data.get('memo', 'なし')}
        ニュース要約: {env_data.get('news_text', 'なし')}
        決算/材料要約: {env_data.get('financial_text', 'なし')}
        日足チャート: {daily_status}

        【今回入力された情報】
        画像1: 5分足チャート（短期トレンド）
        画像2: 板情報（直近の需給）
        補足メモ: {extra_note}

        【指示】
        出力は以下のHTML形式のみで行ってください。余計なマークダウン（```htmlなど）は不要です。
        関西弁で親しみやすく、かつ論理的に記述してください。

        <div class="p-6 bg-white border-2 border-indigo-100 rounded-xl shadow-sm">
            <div class="flex items-center justify-between mb-4 border-b pb-2">
                <span class="text-gray-500 font-bold text-sm">AIジャッジ</span>
                <span class="text-2xl font-black px-4 py-1 rounded bg-gray-800 text-white">
                    {{ここに結論を入れる： 買い / 売り / ホールド / 様子見}}
                </span>
            </div>
            
            <div class="grid grid-cols-2 gap-4 mb-4">
                <div class="bg-blue-50 p-3 rounded text-center">
                    <p class="text-xs text-blue-800 font-bold mb-1">🎯 ターゲット価格</p>
                    <p class="text-lg font-bold text-blue-900">{{利確目標価格}} 円</p>
                </div>
                <div class="bg-red-50 p-3 rounded text-center">
                    <p class="text-xs text-red-800 font-bold mb-1">🛡️ 損切りライン</p>
                    <p class="text-lg font-bold text-red-900">{{損切り価格}} 円</p>
                </div>
            </div>

            <div class="mb-4">
                 <h4 class="font-bold text-gray-700 mb-2">💡 エントリー/アクション範囲</h4>
                 <p class="text-lg font-bold text-indigo-700 bg-indigo-50 p-2 rounded text-center">
                    {{具体的な価格帯：例 1000円〜1005円で拾う}}
                 </p>
            </div>

            <div class="space-y-2 text-sm text-gray-700 leading-relaxed">
                <p><strong>根拠：</strong> {{5分足と板読みからの具体的な根拠を記述}}</p>
                <p><strong>環境認識：</strong> {{日足や材料を考慮した背景情報を記述}}</p>
            </div>
        </div>
        """

        response = model.generate_content([prompt] + images_to_pass)
        result_html = response.text.replace('```html', '').replace('```', '')
        
        stocks_data = {}
        # 修正箇所: is not None を追加
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

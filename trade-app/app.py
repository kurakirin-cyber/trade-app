import os
import requests
import base64
import io
import datetime
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response
from dotenv import load_dotenv
import PIL.Image
from pymongo import MongoClient
from bson.objectid import ObjectId

load_dotenv()

app = Flask(__name__)
app.secret_key = 'super_secret_key_for_stock_app'

# --- MongoDBの設定 ---
MONGO_URI = os.getenv("MONGO_URI")

def get_db_collection():
    if not MONGO_URI: return None
    try:
        client = MongoClient(MONGO_URI)
        db = client['stock_app_db']
        return db['stocks']
    except Exception as e:
        print(f"DB接続エラー: {e}")
        return None

# --- 画像処理 ---
def image_to_base64(img):
    img = img.convert('RGB')
    # 画像サイズをさらに最適化（画質を少し落として軽くする）
    img.thumbnail((800, 800)) 
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG", quality=60)
    return base64.b64encode(buffered.getvalue()).decode('utf-8')

# --- ニュース本文抽出 ---
def fetch_url_content(url_text):
    if not url_text: return ""
    raw_urls = [u.strip() for u in url_text.split('\n') if u.strip().startswith('http')]
    combined_text = ""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }
    for url in raw_urls:
        try:
            resp = requests.get(url, headers=headers, timeout=5) # タイムアウト短縮
            if resp.status_code == 200:
                resp.encoding = resp.apparent_encoding
                soup = BeautifulSoup(resp.content, 'html.parser')
                for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'iframe']):
                    tag.decompose()
                text = soup.get_text(separator='\n', strip=True)
                combined_text += f"\n--- [記事: {url}] ---\n{text[:2000]}...\n"
            else:
                combined_text += f"\n[URL: {url}] アクセス不可 ({resp.status_code})\n"
        except Exception as e:
            combined_text += f"\n[URL: {url}] エラー: {e}\n"
    return combined_text

# --- ルート設定 ---

@app.route('/')
def index():
    """トップページ：画像を除外して軽量データのみ取得"""
    stocks_data = {}
    collection = get_db_collection()
    
    if collection is not None:
        # 【爆速化】画像データ(img_...)を除外して取得する設定
        projection = {"img_daily": 0, "img_5min": 0, "img_board": 0}
        
        try:
            # 最新順に並べて取得
            cursor = collection.find({}, projection).sort("updated_at", -1)
            for doc in cursor:
                if doc.get('code'):
                    doc['_id'] = str(doc['_id'])
                    stocks_data[doc['code']] = doc
        except Exception as e:
            print(f"読み込みエラー: {e}")

    return render_template('index.html', registered_envs=stocks_data)

@app.route('/get_stock/<code_id>')
def get_stock(code_id):
    """詳細データ取得（ここも画像本体は送らない）"""
    collection = get_db_collection()
    if collection is None: return jsonify({}), 500
    
    # 【爆速化】ここでも画像データ自体はDBから引っ張らない
    projection = {"img_daily": 0, "img_5min": 0, "img_board": 0}
    
    # ただし「画像があるかどうか」を知るために、別途チェックが必要なら
    # 本来はDB設計を変えるべきやけど、今回はexistsチェックで対応
    
    data = collection.find_one({"code": code_id}, projection)
    
    if data:
        resp = {k: v for k, v in data.items() if k != '_id'}
        
        # 画像の有無だけ確認するために軽量クエリを投げる（または保存時にフラグを持たせるのが理想）
        # 今回は簡易的に「別クエリで画像の存在確認」をする（データ転送量削減のため）
        img_check = collection.find_one({"code": code_id}, {"img_daily": 1, "img_5min": 1, "img_board": 1})
        
        if img_check:
            resp['has_daily'] = bool(img_check.get('img_daily'))
            resp['has_5min'] = bool(img_check.get('img_5min'))
            resp['has_board'] = bool(img_check.get('img_board'))
        
        return jsonify(resp)
        
    return jsonify({}), 404

@app.route('/save_data', methods=['POST'])
def save_data():
    try:
        collection = get_db_collection()
        if collection is None:
            flash('DBエラー', 'error')
            return redirect(url_for('index'))

        code = request.form.get('code')
        if not code: return redirect(url_for('index'))

        # 既存データの取得（画像維持のため）
        existing = collection.find_one({"code": code}) or {}

        data = {
            "code": code,
            "name": request.form.get('name', ''),
            "updated_at": datetime.datetime.now(),
            "holding_qty": request.form.get('holding_qty', '0'),
            "avg_cost": request.form.get('avg_cost', '0'),
            "target_buy": request.form.get('target_buy', ''),
            "target_sell": request.form.get('target_sell', ''),
            "analysis_memo": request.form.get('analysis_memo', ''),
            "memo": request.form.get('memo', ''),
            "urls": request.form.get('urls', ''),
            "news_content": existing.get('news_content', '')
        }

        if data['urls'] and data['urls'] != existing.get('urls', ''):
            extracted = fetch_url_content(data['urls'])
            if extracted: data['news_content'] = extracted

        # 画像処理
        for img_type in ['img_daily', 'img_5min', 'img_board']:
            file = request.files.get(img_type)
            if file and file.filename:
                data[img_type] = image_to_base64(PIL.Image.open(file))
            else:
                data[img_type] = existing.get(img_type)

        collection.update_one({"code": code}, {"$set": data}, upsert=True)
        flash(f'保存完了！ ({code})', 'success')

    except Exception as e:
        print(e)
        flash(f'保存エラー: {e}', 'error')

    return redirect(url_for('index'))

@app.route('/delete_stock', methods=['POST'])
def delete_stock():
    try:
        collection = get_db_collection()
        code = request.form.get('delete_code')
        if collection is not None and code:
            collection.delete_one({"code": code})
            flash(f'削除完了: {code}', 'success')
    except Exception as e:
        flash(f'削除エラー: {e}', 'error')
    return redirect(url_for('index'))

@app.route('/download_notebooklm/<code_id>')
def download_notebooklm(code_id):
    collection = get_db_collection()
    if collection is None: return "DB Error", 500
    
    data = collection.find_one({"code": code_id})
    if not data: return "Data Not Found", 404

    output = f"【銘柄分析データ: {data.get('name')} ({data.get('code')})】\n"
    output += f"更新日時: {data.get('updated_at').strftime('%Y-%m-%d %H:%M')}\n\n"
    
    output += "■ 保有状況\n"
    output += f"- 株数: {data.get('holding_qty')}\n"
    output += f"- 単価: {data.get('avg_cost')}\n\n"
    
    output += "■ メモ\n"
    output += f"{data.get('memo')}\n\n"
    
    output += "■ ニュース\n"
    output += f"{data.get('news_content')}\n"
    
    return Response(
        output,
        mimetype="text/plain",
        headers={"Content-disposition": f"attachment; filename={code_id}_notebooklm.txt"}
    )

if __name__ == '__main__':
    app.run(debug=True, port=5000)

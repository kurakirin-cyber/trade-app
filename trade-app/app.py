import os
import requests
import base64
import io
import datetime
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, send_file
from dotenv import load_dotenv
import PIL.Image
from pymongo import MongoClient
from bson.objectid import ObjectId
from bson.errors import InvalidId

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "default_dev_secret_key")

# --- MongoDBの設定 ---
MONGO_URI = os.getenv("MONGO_URI")
mongo_client = None
mongo_db = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI, connect=False)
        mongo_db = mongo_client['stock_app_db']
        print("✅ MongoDB接続成功")
    except Exception as e:
        print(f"❌ MongoDB接続初期化エラー: {e}")

def get_db_collection():
    if mongo_db is not None:
        return mongo_db['stocks']
    return None

# --- 画像処理 ---
def image_to_base64(img):
    img = img.convert('RGB')
    img.thumbnail((1024, 1024)) 
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG", quality=70)
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
            resp = requests.get(url, headers=headers, timeout=3)
            if resp.status_code == 200:
                resp.encoding = resp.apparent_encoding
                soup = BeautifulSoup(resp.content, 'html.parser')
                for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'iframe', 'svg']):
                    tag.decompose()
                text = soup.get_text(separator='\n', strip=True)
                combined_text += f"\n--- [記事: {url}] ---\n{text[:2000]}...\n"
            else:
                combined_text += f"\n[URL: {url}] アクセス不可 ({resp.status_code})\n"
        except Exception as e:
            combined_text += f"\n[URL: {url}] 取得スキップ: {str(e)[:50]}...\n"
    return combined_text

# --- ルート設定 ---

@app.route('/')
def index():
    stocks_data = {}
    collection = get_db_collection()
    
    if collection is not None:
        pipeline = [
            {"$sort": {"updated_at": -1}},
            {"$group": {
                "_id": "$code",
                "doc": {"$first": "$$ROOT"}
            }},
            {"$replaceRoot": {"newRoot": "$doc"}},
            {"$sort": {"updated_at": -1}}
        ]
        try:
            cursor = collection.aggregate(pipeline)
            for doc in cursor:
                if doc.get('code'):
                    doc['_id'] = str(doc['_id'])
                    # Base64画像は一覧には含めない
                    if 'img_daily' in doc: del doc['img_daily']
                    if 'img_5min' in doc: del doc['img_5min']
                    if 'img_board' in doc: del doc['img_board']
                    stocks_data[doc['code']] = doc
        except Exception as e:
            print(f"集計エラー: {e}")

    return render_template('index.html', registered_envs=stocks_data)

@app.route('/get_history/<code_id>')
def get_history(code_id):
    collection = get_db_collection()
    if collection is None: return jsonify([]), 500
    
    cursor = collection.find({"code": code_id}, {"updated_at": 1, "_id": 1}).sort("updated_at", -1)
    
    history = []
    for doc in cursor:
        date_str = "不明な日時"
        if doc.get('updated_at'):
            date_str = doc['updated_at'].strftime('%Y/%m/%d %H:%M')
            
        history.append({
            "id": str(doc['_id']),
            "date": date_str
        })
    return jsonify(history)

@app.route('/get_log/<log_id>')
def get_log(log_id):
    collection = get_db_collection()
    if collection is None: return jsonify({}), 500
    
    try:
        data = collection.find_one({"_id": ObjectId(log_id)})
        if data:
            resp = {k: v for k, v in data.items() if k != '_id'}
            resp['id'] = str(data['_id'])
            
            resp['has_daily'] = bool(resp.get('img_daily'))
            resp['has_5min'] = bool(resp.get('img_5min'))
            resp['has_board'] = bool(resp.get('img_board'))
            
            if 'img_daily' in resp: del resp['img_daily']
            if 'img_5min' in resp: del resp['img_5min']
            if 'img_board' in resp: del resp['img_board']
            
            return jsonify(resp)
    except Exception as e:
        print(f"Log取得エラー: {e}")
        
    return jsonify({}), 404

@app.route('/image/<log_id>/<img_type>')
def get_image(log_id, img_type):
    collection = get_db_collection()
    if collection is None: return "DB Error", 500
    
    if img_type not in ['img_daily', 'img_5min', 'img_board']:
        return "Invalid Image Type", 400

    try:
        data = collection.find_one({"_id": ObjectId(log_id)}, {img_type: 1})
        if data and data.get(img_type):
            img_data = base64.b64decode(data[img_type])
            return send_file(io.BytesIO(img_data), mimetype='image/jpeg')
        else:
            return "Image Not Found", 404
    except Exception as e:
        return f"Error: {e}", 500

@app.route('/save_data', methods=['POST'])
def save_data():
    try:
        collection = get_db_collection()
        if collection is None:
            flash('DBエラー: 接続できてへんで', 'error')
            return redirect(url_for('index'))

        code = request.form.get('code')
        log_id = request.form.get('log_id')
        new_urls = request.form.get('urls', '').strip()
        
        if not code: return redirect(url_for('index'))

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
            "urls": new_urls,
            "news_content": ""
        }

        existing_doc = {}
        if log_id:
            existing_doc = collection.find_one({"_id": ObjectId(log_id)}) or {}
            
            old_urls = existing_doc.get("urls", "").strip()
            old_content = existing_doc.get("news_content", "")
            
            if new_urls == old_urls and old_content:
                data["news_content"] = old_content
            else:
                if new_urls:
                    data['news_content'] = fetch_url_content(new_urls)
        else:
            if new_urls:
                data['news_content'] = fetch_url_content(new_urls)

        for img_type in ['img_daily', 'img_5min', 'img_board']:
            file = request.files.get(img_type)
            if file and file.filename:
                data[img_type] = image_to_base64(PIL.Image.open(file))
            elif log_id and existing_doc:
                data[img_type] = existing_doc.get(img_type)

        if log_id:
            collection.update_one({"_id": ObjectId(log_id)}, {"$set": data})
            flash(f'履歴を修正・保存したで！', 'success')
        else:
            collection.insert_one(data)
            flash(f'新しい履歴を追加したで！', 'success')

    except Exception as e:
        print(e)
        flash(f'保存エラー: {e}', 'error')

    return redirect(url_for('index'))

@app.route('/delete_log', methods=['POST'])
def delete_log():
    try:
        collection = get_db_collection()
        log_id = request.form.get('delete_log_id')
        if collection is not None and log_id:
            collection.delete_one({"_id": ObjectId(log_id)})
            flash('履歴を1件削除したで', 'success')
    except Exception as e:
        flash(f'削除エラー: {e}', 'error')
    return redirect(url_for('index'))

@app.route('/download_notebooklm/<log_id>')
def download_notebooklm(log_id):
    collection = get_db_collection()
    if collection is None: return "DB Error", 500
    
    try:
        if not log_id: raise ValueError("IDが空や")
        
        data = collection.find_one({"_id": ObjectId(log_id)})
        if not data: return "Data Not Found", 404

        # 日付処理の安全対策
        date_str = "不明"
        if data.get('updated_at'):
            date_str = data.get('updated_at').strftime('%Y-%m-%d %H:%M')

        output = f"【銘柄分析データ: {data.get('name', '名称未設定')} ({data.get('code', 'NoCode')})】\n"
        output += f"記録日時: {date_str}\n\n"
        
        output += "■ 現在の保有状況\n"
        output += f"- 保有株数: {data.get('holding_qty', '0')}株\n"
        output += f"- 平均取得単価: {data.get('avg_cost', '0')}円\n\n"
        
        output += "■ ユーザーのメモ・環境認識\n"
        output += f"{data.get('memo', '')}\n\n"
        
        output += "■ 関連ニュース・開示情報\n"
        news = data.get('news_content', '')
        output += f"{news if news else '（ニュース情報なし）'}\n"
        
        output += "\n" + "="*30 + "\n"
        output += "■ NotebookLMへの指示 (System Prompt)\n"
        output += "あなたはプロの株式トレーダーのアシスタントです。上記のデータを分析し、以下の点について具体的な助言を行ってください。\n"
        output += "1. 現状の保有ポジション（含み益/含み損）に基づいた、最適な決済（利確・損切り）の目安価格。\n"
        output += "2. ニュース材料とユーザーのメモから読み取れる、今後の株価シナリオ（楽観・悲観の両方）。\n"
        output += "3. 新規エントリーまたはナンピンを検討する場合の推奨価格帯。\n"
        
        return Response(
            output,
            mimetype="text/plain",
            headers={"Content-disposition": f"attachment; filename={data.get('code')}_notebooklm.txt"}
        )
    except InvalidId:
        return "Error: 無効なID形式です", 400
    except Exception as e:
        print(f"Copy Error: {e}")
        return f"Error: {e}", 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)

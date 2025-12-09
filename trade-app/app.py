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
from bson.objectid import ObjectId # これが必要

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
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                resp.encoding = resp.apparent_encoding
                soup = BeautifulSoup(resp.content, 'html.parser')
                for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'iframe']):
                    tag.decompose()
                text = soup.get_text(separator='\n', strip=True)
                combined_text += f"\n--- [記事: {url}] ---\n{text[:3000]}...\n"
            else:
                combined_text += f"\n[URL: {url}] アクセス不可 ({resp.status_code})\n"
        except Exception as e:
            combined_text += f"\n[URL: {url}] エラー: {e}\n"
    return combined_text

# --- ルート設定 ---

@app.route('/')
def index():
    """トップページ：銘柄リスト（最新のもの）を表示"""
    stocks_data = {}
    collection = get_db_collection()
    
    if collection is not None:
        # 銘柄ごとにグルーピングして、一番新しいデータを1つずつ取得する（集計クエリ）
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
                    # IDを文字列に変換しておく
                    doc['_id'] = str(doc['_id'])
                    stocks_data[doc['code']] = doc
        except Exception as e:
            print(f"集計エラー: {e}")

    return render_template('index.html', registered_envs=stocks_data)

@app.route('/get_history/<code_id>')
def get_history(code_id):
    """指定した銘柄の履歴リスト（日時とID）を返す"""
    collection = get_db_collection()
    if not collection: return jsonify([]), 500
    
    # その銘柄のデータを新しい順に全部取得
    cursor = collection.find({"code": code_id}, {"updated_at": 1, "_id": 1}).sort("updated_at", -1)
    
    history = []
    for doc in cursor:
        history.append({
            "id": str(doc['_id']),
            "date": doc['updated_at'].strftime('%Y/%m/%d %H:%M') if doc.get('updated_at') else "不明な日時"
        })
    return jsonify(history)

@app.route('/get_log/<log_id>')
def get_log(log_id):
    """特定の履歴データの詳細を返す"""
    collection = get_db_collection()
    if not collection: return jsonify({}), 500
    
    try:
        data = collection.find_one({"_id": ObjectId(log_id)})
        if data:
            resp = {k: v for k, v in data.items() if k != '_id'}
            resp['id'] = str(data['_id']) # 文字列IDを含める
            
            # 画像有無フラグ
            resp['has_daily'] = bool(resp.get('img_daily'))
            resp['has_5min'] = bool(resp.get('img_5min'))
            resp['has_board'] = bool(resp.get('img_board'))
            
            # 画像データは除外（軽量化）
            if 'img_daily' in resp: del resp['img_daily']
            if 'img_5min' in resp: del resp['img_5min']
            if 'img_board' in resp: del resp['img_board']
            
            return jsonify(resp)
    except Exception as e:
        print(f"Log取得エラー: {e}")
        
    return jsonify({}), 404

@app.route('/save_data', methods=['POST'])
def save_data():
    """データの保存（新規または上書き）"""
    try:
        collection = get_db_collection()
        if not collection:
            flash('DBエラー', 'error')
            return redirect(url_for('index'))

        code = request.form.get('code')
        log_id = request.form.get('log_id') # 編集時はこれが入ってくる
        
        if not code: return redirect(url_for('index'))

        # ベースデータの構築
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
            "news_content": "" # 後で設定
        }

        # 編集モード（log_idあり）の場合、既存データを一度取得して画像などを引き継ぐ
        existing_doc = {}
        if log_id:
            existing_doc = collection.find_one({"_id": ObjectId(log_id)}) or {}
            # 既存のニュース内容を引き継ぎ（URL再取得しない場合のため）
            data["news_content"] = existing_doc.get("news_content", "")
        else:
            # 新規の場合、同じ銘柄の「最新データ」から基本情報を引き継ぐと便利かも（今回はシンプルに空で）
            pass

        # ニュース取得ロジック
        if data['urls']:
            # URLが変わったか、強制更新したい場合だが、シンプルに毎回取得する
            extracted = fetch_url_content(data['urls'])
            if extracted:
                data['news_content'] = extracted

        # 画像処理
        for img_type in ['img_daily', 'img_5min', 'img_board']:
            file = request.files.get(img_type)
            if file and file.filename:
                # 新しい画像がアップされたら変換
                data[img_type] = image_to_base64(PIL.Image.open(file))
            elif log_id and existing_doc:
                # 編集モードで画像変更なしなら、既存の画像を維持
                data[img_type] = existing_doc.get(img_type)
            # 新規で画像なしならNoneのまま

        if log_id:
            # 【編集モード】既存のレコードを上書き更新
            collection.update_one({"_id": ObjectId(log_id)}, {"$set": data})
            flash(f'履歴を修正・保存したで！ ({data["updated_at"].strftime("%H:%M")})', 'success')
        else:
            # 【新規モード】新しいレコードとして追加
            collection.insert_one(data)
            flash(f'新しい履歴を追加したで！ ({code})', 'success')

    except Exception as e:
        print(e)
        flash(f'保存エラー: {e}', 'error')

    return redirect(url_for('index'))

@app.route('/delete_log', methods=['POST'])
def delete_log():
    """特定の履歴を削除"""
    try:
        collection = get_db_collection()
        log_id = request.form.get('delete_log_id')
        if collection and log_id:
            collection.delete_one({"_id": ObjectId(log_id)})
            flash('履歴を1件削除したで', 'success')
    except Exception as e:
        flash(f'削除エラー: {e}', 'error')
    return redirect(url_for('index'))

@app.route('/download_notebooklm/<log_id>')
def download_notebooklm(log_id):
    """特定ログの内容をテキスト化してダウンロード"""
    collection = get_db_collection()
    if not collection: return "DB Error", 500
    
    try:
        data = collection.find_one({"_id": ObjectId(log_id)})
        if not data: return "Data Not Found", 404

        output = f"【銘柄分析データ: {data.get('name')} ({data.get('code')})】\n"
        output += f"記録日時: {data.get('updated_at').strftime('%Y-%m-%d %H:%M')}\n\n"
        
        output += "■ 現在の保有状況\n"
        output += f"- 保有株数: {data.get('holding_qty')}株\n"
        output += f"- 平均取得単価: {data.get('avg_cost')}円\n\n"
        
        output += "■ ユーザーのメモ・環境認識\n"
        output += f"{data.get('memo')}\n\n"
        
        output += "■ 関連ニュース・開示情報\n"
        output += f"{data.get('news_content')}\n"
        
        return Response(
            output,
            mimetype="text/plain",
            headers={"Content-disposition": f"attachment; filename={data.get('code')}_notebooklm.txt"}
        )
    except Exception as e:
        return f"Error: {e}", 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)

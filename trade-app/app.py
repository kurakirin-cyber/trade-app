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

# --- ヘルパー関数 ---

def image_to_base64(img):
    """画像をBase64に変換"""
    img = img.convert('RGB')
    img.thumbnail((1024, 1024)) 
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG", quality=70)
    return base64.b64encode(buffered.getvalue()).decode('utf-8')

def shorten_url_api(long_url):
    """TinyURLを使ってURLを短縮する"""
    if not long_url or len(long_url) < 30: return long_url # 短いならそのまま
    try:
        # タイムアウト1秒でサクッと取得
        api_url = f"http://tinyurl.com/api-create.php?url={long_url}"
        res = requests.get(api_url, timeout=2)
        if res.status_code == 200:
            return res.text
    except:
        pass # 失敗したら元のURLを返す
    return long_url

def process_urls(url_text):
    """URL欄のテキストを改行で分けて、それぞれ短縮処理する"""
    if not url_text: return ""
    urls = [u.strip() for u in url_text.split('\n') if u.strip()]
    shortened_list = []
    
    # 全部短縮APIにかける（少し時間がかかるので注意）
    for u in urls:
        if u.startswith('http'):
            shortened_list.append(shorten_url_api(u))
        else:
            shortened_list.append(u)
            
    return '\n'.join(shortened_list)

def fetch_url_content(url_text):
    """ニュース本文抽出"""
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
    
    # 保存直後のリダイレクトで指定された銘柄コードを受け取る
    active_code = request.args.get('active_code', '')

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
                    # 重いデータは一覧から除外
                    for heavy_key in ['img_daily', 'img_5min', 'img_board', 'pdf_file']:
                        if heavy_key in doc: del doc[heavy_key]
                    stocks_data[doc['code']] = doc
        except Exception as e:
            print(f"集計エラー: {e}")

    return render_template('index.html', registered_envs=stocks_data, active_code=active_code)

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
        history.append({"id": str(doc['_id']), "date": date_str})
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
            
            # バイナリデータ有無フラグ
            resp['has_daily'] = bool(resp.get('img_daily'))
            resp['has_5min'] = bool(resp.get('img_5min'))
            resp['has_board'] = bool(resp.get('img_board'))
            resp['has_pdf']   = bool(resp.get('pdf_file')) # PDFフラグ追加
            
            # データ本体は削除して軽量化
            for heavy_key in ['img_daily', 'img_5min', 'img_board', 'pdf_file']:
                if heavy_key in resp: del resp[heavy_key]
            
            return jsonify(resp)
    except Exception as e:
        print(f"Log取得エラー: {e}")
        
    return jsonify({}), 404

@app.route('/file/<log_id>/<file_type>')
def get_file(log_id, file_type):
    """画像またはPDFをダウンロード/表示する"""
    collection = get_db_collection()
    if collection is None: return "DB Error", 500
    
    # 許可するフィールド名
    allowed_types = ['img_daily', 'img_5min', 'img_board', 'pdf_file']
    if file_type not in allowed_types: return "Invalid Type", 400

    try:
        data = collection.find_one({"_id": ObjectId(log_id)}, {file_type: 1, "code": 1})
        if data and data.get(file_type):
            file_data = base64.b64decode(data[file_type])
            
            # PDFの場合
            if file_type == 'pdf_file':
                return Response(
                    file_data,
                    mimetype='application/pdf',
                    headers={"Content-Disposition": f"inline; filename={data.get('code')}_doc.pdf"}
                )
            # 画像の場合
            else:
                return send_file(io.BytesIO(file_data), mimetype='image/jpeg')
        else:
            return "File Not Found", 404
    except Exception as e:
        return f"Error: {e}", 500

@app.route('/save_data', methods=['POST'])
def save_data():
    collection = get_db_collection()
    if collection is None:
        flash('DBエラー: 接続できてへんで', 'error')
        return redirect(url_for('index'))

    try:
        code = request.form.get('code')
        log_id = request.form.get('log_id')
        raw_urls = request.form.get('urls', '').strip()
        
        if not code: return redirect(url_for('index'))

        # URLの自動短縮処理
        # (新規保存、またはURLが変更された時のみ実行してAPI負荷を下げる)
        # 簡易的に「とりあえず毎回短縮試行」する形にする（既存の短いURLはそのまま返るため）
        processed_urls = process_urls(raw_urls)

        data = {
            "code": code,
            "name": request.form.get('name', ''),
            "updated_at": datetime.datetime.now(),
            "current_price": request.form.get('current_price', ''), # 現在値
            "holding_qty": request.form.get('holding_qty', '0'),
            "avg_cost": request.form.get('avg_cost', '0'),
            "target_buy": request.form.get('target_buy', ''),
            "target_sell": request.form.get('target_sell', ''),
            "analysis_memo": request.form.get('analysis_memo', ''),
            "memo": request.form.get('memo', ''),
            "urls": processed_urls, # 短縮済みのURLを保存
            "news_content": ""
        }

        existing_doc = {}
        if log_id:
            existing_doc = collection.find_one({"_id": ObjectId(log_id)}) or {}
            
            # ニュース本文の再取得判定
            old_urls = existing_doc.get("urls", "").strip()
            old_content = existing_doc.get("news_content", "")
            
            # URLが変わってないなら記事本文は使い回す
            # (processed_urlsと比較すると短縮前後でズレるので、raw_urlsも考慮したいが、
            #  シンプルに「URL欄が空でなければ取得」にする)
            if raw_urls and (processed_urls != old_urls):
                 data['news_content'] = fetch_url_content(raw_urls)
            elif old_content:
                 data['news_content'] = old_content
        else:
            # 新規
            if raw_urls:
                data['news_content'] = fetch_url_content(raw_urls)

        # 画像保存
        for img_type in ['img_daily', 'img_5min', 'img_board']:
            file = request.files.get(img_type)
            if file and file.filename:
                data[img_type] = image_to_base64(PIL.Image.open(file))
            elif log_id and existing_doc:
                data[img_type] = existing_doc.get(img_type)

        # PDF保存
        pdf = request.files.get('pdf_file')
        if pdf and pdf.filename:
            # サイズチェック (例えば10MB制限)
            pdf.seek(0, os.SEEK_END)
            size = pdf.tell()
            if size > 10 * 1024 * 1024:
                flash('PDFデカすぎや！10MB以下にしてな', 'error')
                # PDF以外は保存せず戻る（あるいはPDFだけ無視するか）
                # ここでは安全のため処理を中断してリダイレクト
                return redirect(url_for('index', active_code=code))
            
            pdf.seek(0)
            data['pdf_file'] = base64.b64encode(pdf.read()).decode('utf-8')
        elif log_id and existing_doc:
             data['pdf_file'] = existing_doc.get('pdf_file')

        if log_id:
            collection.update_one({"_id": ObjectId(log_id)}, {"$set": data})
            flash(f'修正保存したで！（URL短縮済）', 'success')
        else:
            collection.insert_one(data)
            flash(f'新規追加したで！（URL短縮済）', 'success')

    except Exception as e:
        print(e)
        flash(f'保存エラー: {e}', 'error')

    # 【重要】保存していた銘柄コードをパラメータとして渡してリダイレクト
    return redirect(url_for('index', active_code=code))

@app.route('/delete_log', methods=['POST'])
def delete_log():
    code = ""
    try:
        collection = get_db_collection()
        log_id = request.form.get('delete_log_id')
        if collection is not None and log_id:
            # 削除前にコードを取得（リダイレクト用）
            doc = collection.find_one({"_id": ObjectId(log_id)})
            if doc: code = doc.get('code', '')
            
            collection.delete_one({"_id": ObjectId(log_id)})
            flash('履歴を1件削除したで', 'success')
    except Exception as e:
        flash(f'削除エラー: {e}', 'error')
    
    return redirect(url_for('index', active_code=code))

@app.route('/download_notebooklm/<log_id>')
def download_notebooklm(log_id):
    collection = get_db_collection()
    if collection is None: return "DB Error", 500
    
    try:
        if not log_id: raise ValueError("IDが空や")
        data = collection.find_one({"_id": ObjectId(log_id)})
        if not data: return "Data Not Found", 404

        date_str = "不明"
        if data.get('updated_at'):
            date_str = data.get('updated_at').strftime('%Y-%m-%d %H:%M')

        output = f"【銘柄分析データ: {data.get('name', '名称未設定')} ({data.get('code', 'NoCode')})】\n"
        output += f"記録日時: {date_str}\n\n"
        
        output += "■ 価格・保有状況\n"
        output += f"- 現在値: {data.get('current_price', '不明')}円\n"
        output += f"- 保有株数: {data.get('holding_qty', '0')}株\n"
        output += f"- 平均取得単価: {data.get('avg_cost', '0')}円\n\n"
        
        output += "■ ユーザーのメモ・環境認識\n"
        output += f"{data.get('memo', '')}\n\n"
        
        # PDF添付の有無をAIに伝える
        if data.get('pdf_file'):
            output += "※このデータには決算書などのPDFファイルが添付されています。人間は確認できますが、このテキストデータには含まれていません。\n\n"

        output += "■ 関連ニュース・開示情報 (URL短縮済)\n"
        output += f"URLリスト: {data.get('urls', '')}\n" # URLも出力
        news = data.get('news_content', '')
        output += f"{news if news else '（ニュース情報なし）'}\n"
        
        output += "\n" + "="*30 + "\n"
        output += "■ NotebookLMへの指示 (System Prompt)\n"
        output += "あなたはプロの株式トレーダーのアシスタントです。上記のデータを分析し、以下の点について具体的な助言を行ってください。\n"
        output += "1. 「現在値」と「平均取得単価」を比較し、現状の含み損益を考慮した上で、最適な決済（利確・損切り）の目安価格を提示。\n"
        output += "2. ニュース材料とメモから読み取れる、今後の株価シナリオ。\n"
        output += "3. 新規エントリーまたはナンピンを検討する場合の推奨価格帯。\n"
        
        return Response(
            output,
            mimetype="text/plain",
            headers={"Content-disposition": f"attachment; filename={data.get('code')}_notebooklm.txt"}
        )
    except Exception as e:
        return f"Error: {e}", 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)

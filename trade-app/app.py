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

# Geminiの設定
GENAI_API_KEY = os.getenv("GEMINI_API_KEY")
if GENAI_API_KEY:
    genai.configure(api_key=GENAI_API_KEY)

# 【重要】一旦ここでモデルは定義せず、使う直前に呼び出す形に変更
# (エラーハンドリングをしやすくするため)

# 簡易データベース
STOCKS_DB = {}

def fetch_url_content(url_text):
    """URLからテキスト情報を引っこ抜く関数"""
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
    """5分足と板画像＋保存情報の統合判断"""
    
    if request.method == 'GET':
        return redirect(url_for('index'))

    try:
        if not GENAI_API_KEY:
            flash('APIキーが設定されてへんで！Renderの設定画面で GEMINI_API_KEY を入れてな。', 'error')
            return redirect(url_for('index'))

        # フォーム入力
        code = request.form.get('stock_code')
        extra_note = request.form.get('extra_note')
        chart_file = request.files.get('chart_image')
        board_file = request.files.get('orderbook_image')

        if not chart_file or not board_file:
            flash('画像は2枚とも必須やで！', 'error')
            return redirect(url_for('index'))

        # 環境情報
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

        # プロンプト
        prompt = f"""
        あなたは超一流のデイトレーダーです。
        以下の情報に基づき、**HTML形式**で見やすく判断を出力してください。
        
        【入力情報】
        1. 5分足チャート画像
        2. 板情報画像
        3. 補足メモ: {extra_note}
        4. 環境情報:
        {env_context}

        【出力要件】
        JSONではなく、Webページ用の**HTMLタグ**のみを出力（```html 不要）。
        - <div class="p-4 bg-gray-50 rounded-lg"> で囲む。
        - 結論（<h3>タグ、class="text-2xl font-bold mb-2"、BUY/SELL/WAITを強調）
        - エントリー・利確・損切りの数値（<ul>リスト）
        - 根拠（<p>タグ）
        - リスク注意点
        
        関西弁で出力してください。
        """

        # 【診断機能付きモデル実行】
        # まずは gemini-1.5-flash を試す
        target_model_name = 'gemini-1.5-flash'
        
        try:
            model = genai.GenerativeModel(target_model_name)
            response = model.generate_content([prompt, chart_img, board_img])
            result_html = response.text.replace('```html', '').replace('```', '')

            return render_template('index.html', 
                                 judge_result=result_html,
                                 registered_envs=STOCKS_DB,
                                 form_values={'stock_code': code, 'extra_note': extra_note})

        except Exception as api_error:
            # もしエラーが出たら、使えるモデル一覧を取得してエラーメッセージに表示する
            error_msg = str(api_error)
            
            # 404 Not Found (モデルが見つからない) 系のエラーの場合
            if "404" in error_msg or "not found" in error_msg.lower():
                available_models = []
                try:
                    for m in genai.list_models():
                        if 'generateContent' in m.supported_generation_methods:
                            available_models.append(m.name)
                except:
                    available_models = ["一覧取得失敗"]

                # 画面にリストを表示してあげる
                flash(f"【モデル設定エラー】'{target_model_name}' が使えへんみたいや。\n"
                      f"今使えるモデル一覧はこれやで: {', '.join(available_models)}\n"
                      f"コード内のモデル名をこれに合わせて書き換えてな。", 'error')
            else:
                # その他のエラー
                flash(f'AI呼び出しエラー: {error_msg}', 'error')
            
            return redirect(url_for('index'))

    except Exception as e:
        print(f"Error: {e}")
        flash(f'システムエラー: {str(e)}', 'error')
        return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)

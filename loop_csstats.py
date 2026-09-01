from flask import Flask, request, jsonify
from cs_class import *

app = Flask(__name__)

@app.route('/', methods=['POST'])
def main():
    # Проверяем, что пришёл JSON
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 400

    data = request.get_json()  # безопаснее, чем request.json

    # Логируем
    try:
        with open('log.txt', 'a', encoding='utf-8') as f:
            # dump сразу в файл (не dumps!)
            import json
            json.dump(data, f, ensure_ascii=False)
            f.write('\n')  # чтобы каждый запрос с новой строки
    except PermissionError:
        # Если нет прав на append, пробуем создать файл заново (только если совсем никак)
        try:
            with open('log.txt', 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
                f.write('\n')
        except Exception:
            # Если вообще никак не можем записать — хотя бы не ломаем API
            pass  # или логируй в stderr

    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(host="127.0.0.1", port=59873)

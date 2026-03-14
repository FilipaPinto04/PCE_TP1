from flask import Flask, jsonify

app = Flask(__name__)

# Definição da rota GET /healthcheck
@app.route('/healthcheck', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "message": "O serviço está rodando corretamente!",
        "version": "1.0.0"
    }), 200

if __name__ == '__main__':
    # O serviço rodará em http://localhost:5000
    app.run(host='0.0.0.0', port=5000, debug=True)
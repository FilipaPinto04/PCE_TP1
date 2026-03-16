import requests
import json

# --- 1. A FUNÇÃO DE TRADUÇÃO (O teu "Mapeador") ---
def traduzir_paciente(dados):
    partes_nome = dados['nome'].split(' ')
    primeiro_nome = partes_nome[0]
    apelido = ' '.join(partes_nome[1:]) if len(partes_nome) > 1 else ""

    # Converte o teu JSON para o Formato FHIR R4
    return {
        "resourceType": "Patient",
        "name": [{"family": apelido, "given": [primeiro_nome]}],
        "gender": "male" if dados['genero'] == 'm' else "female",
        "telecom": [
            {
                "system": "phone" if "telemóvel" in t['tipo'] else "email",
                "value": t['valor']
            } for t in dados.get('telecom', [])
        ],
        "contact": [
            {
                "name": {"text": c['nome']},
                "telecom": [{"system": "phone", "value": tel['valor']} for tel in c.get('telecom', [])]
            } for c in dados.get('contacto', [])
        ]
    }

# --- 2. O SCRIPT AUTOMÁTICO ---

# O teu ficheiro original (podes ler de um ficheiro .json ou definir aqui)
meu_formato = "./exemplo_post_paciente.json"

# Passo A: Traduzir automaticamente
dados_fhir = traduzir_paciente(meu_formato)

# Passo B: Enviar para o HAPI FHIR
# Nota: A porta 9000 é a que definiste no teu docker-compose
url = "http://localhost:9000/fhir/Patient"
headers = {"Content-Type": "application/fhir+json"}

print(f"A enviar dados para {url}...")

try:
    response = requests.post(url, json=dados_fhir, headers=headers)
    
    if response.status_code == 201:
        print("Sucesso! Paciente criado no HAPI FHIR.")
        print(f"ID atribuído: {response.json()['id']}")
    else:
        print(f"Erro no Servidor: {response.status_code}")
        print(response.text)
except Exception as e:
    print(f"Erro de ligação: {e}")
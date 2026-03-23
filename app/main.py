import psycopg2
import requests
from fastapi import FastAPI, HTTPException
from psycopg2.extras import RealDictCursor
import os

app = FastAPI()

def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=5432,
        database="tp1",
        user="admin",
        password="admin"
    )

@app.post("/Patient")
async def create_patient(data: dict):
    conn = None
    try:
        nome_completo = data.get('nome', 'Sem Nome')
        genero = data.get('genero', 'unknown')
        tipo_telecom = data.get('telecom/tipo') #FALTA
        valor_telecom = data.get('telecom/valor') #FALTA

        # --- PASSO A: GUARDAR NO POSTGRES ---
        conn = get_db_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:  # <-- aqui
            cur.execute(
                "INSERT INTO patients (nome, genero) VALUES (%s, %s) RETURNING id",
                (nome_completo, genero)
            )
            cur.execute(
                "INSERT INTO telecom (tipo, valor) VALUES (%s, %s) RETURNING id",
                (tipo_telecom, valor_telecom)
            )
            sql_id = cur.fetchone()['id']
            conn.commit()

        # --- PASSO B: MANDAR PARA O HAPI ---
        fhir_payload = {
            "resourceType": "Patient",
            "name": [{"given": [nome_completo]}],
            "gender": "male" if genero == "m" else "female" if genero == "f" else "unknown"
        }

        hapi_url = os.getenv("HAPI_URL", "http://localhost:8080/fhir/Patient")  # <-- aqui

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, timeout=5)
            hapi_status = hapi_res.status_code
        except:
            hapi_status = "HAPI offline"

        return {
            "mensagem": "Processado com sucesso",
            "sql_id": sql_id,
            "hapi_server_status": hapi_status
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro: {str(e)}")
    finally:
        if conn:
            conn.close()
import psycopg2
import requests
from fastapi import FastAPI, HTTPException
from psycopg2.extras import RealDictCursor
import os

app = FastAPI()

def get_db_connection():
    # Nota: Certifica-te que estas credenciais coincidem com o teu docker-compose
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", 5432),
        database=os.getenv("DB_NAME", "tp1"),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASS", "admin")
    )

def init_db():
    conn = None
    try:
        conn = get_db_connection()
        # Procura o ficheiro schema.sql na mesma pasta
        schema_path = os.path.join(os.path.dirname(__file__), "../db/schema.sql")
        
        if os.path.exists(schema_path):
            with open(schema_path, "r", encoding="utf-8") as f:
                sql_schema = f.read()
            
            with conn.cursor() as cur:
                cur.execute(sql_schema)
                conn.commit()
                print("✅ Tabelas inicializadas com sucesso via schema.sql!")
        else:
            print("⚠️ Ficheiro schema.sql não encontrado. Ignorando init_db.")
            
    except Exception as e:
        print(f"❌ Erro ao inicializar base de dados: {e}")
    finally:
        if conn:
            conn.close()

@app.on_event("startup")
async def startup_event():
    init_db()

@app.post("/Patient")
async def create_patient(data: dict):
    conn = None
    try:
        # Extrair dados base para usar mais tarde no FHIR
        nome_paciente = data.get('nome', 'Sem Nome')
        genero_raw = data.get('genero', 'unknown')

        conn = get_db_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # --- 1. Inserir Paciente (António) ---
            cur.execute(
                "INSERT INTO patients (nome, genero) VALUES (%s, %s) RETURNING id",
                (nome_paciente, genero_raw)
            )
            paciente_id = cur.fetchone()['id']

            # --- 2. Inserir Telecoms do Paciente ---
            for tel in data.get('telecom', []):
                cur.execute(
                    "INSERT INTO telecom (paciente_id, tipo, valor) VALUES (%s, %s, %s)",
                    (paciente_id, tel.get('tipo'), tel.get('valor'))
                )

            # --- 3. Inserir Contactos (Maria) ---
            for con in data.get('contacto', []):
                cur.execute(
                    "INSERT INTO contacto (paciente_id, nome) VALUES (%s, %s) RETURNING id",
                    (paciente_id, con.get('nome'))
                )
                contacto_id = cur.fetchone()['id']

                # Telecoms da Maria
                for tel_con in con.get('telecom', []):
                    cur.execute(
                        "INSERT INTO telecom (contacto_id, tipo, valor) VALUES (%s, %s, %s)",
                        (contacto_id, tel_con.get('tipo'), tel_con.get('valor'))
                    )
                
                # Endereços da Maria
                for end in con.get('endereco', []):
                    cur.execute(
                        "INSERT INTO endereco (contacto_id, tipo, valor) VALUES (%s, %s, %s)",
                        (contacto_id, end.get('tipo'), end.get('valor'))
                    )

            conn.commit()

        # --- PASSO B: MANDAR PARA O HAPI (FHIR) ---

        # 1. Mapear Telecoms do Paciente
        fhir_telecoms = []
        for t in data.get('telecom', []):
            sistema = "phone" if t.get('tipo') == "telemóvel" else "email"
            fhir_telecoms.append({"system": sistema, "value": t.get('valor')})

        # 2. Mapear Contactos de Emergência
        fhir_contacts = []
        for con in data.get('contacto', []):
            con_telecoms = [
                {"system": "phone" if tc.get('tipo') == "telemóvel" else "email", "value": tc.get('valor')}
                for tc in con.get('telecom', [])
            ]
            
            con_addresses = [
                {"use": "home" if addr.get('tipo') == "casa" else "work", "line": [addr.get('valor')]}
                for addr in con.get('endereco', [])
            ]

            fhir_contacts.append({
                "relationship": [{"text": "Emergency Contact"}],
                "name": {"family": con.get('nome')},
                "telecom": con_telecoms,
                "address": con_addresses
            })

        # 3. Montar o Recurso Patient Final (Corrigido keys FHIR)
        fhir_payload = {
            "resourceType": "Patient",
            "name": [{"text": nome_paciente}],
            "gender": "male" if genero_raw == "m" else "female" if genero_raw == "f" else "unknown",
            "telecom": fhir_telecoms,
            "contact": fhir_contacts
        }

        # 4. Envio para o Servidor HAPI
        hapi_url = os.getenv("HAPI_URL", "http://localhost:8080/fhir/Patient")
        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, timeout=5)
            hapi_status = hapi_res.status_code
        except Exception as req_err:
            print(f"Erro na ligação ao HAPI: {req_err}")
            hapi_status = "HAPI offline"

        return {
            "mensagem": "Processado com sucesso",
            "sql_id": paciente_id,
            "hapi_server_status": hapi_status
        }

    except Exception as e:
        if conn: conn.rollback()
        print(f"Erro detetado: {e}")
        raise HTTPException(status_code=500, detail=f"Erro: {str(e)}")
    finally:
        if conn:
            conn.close()
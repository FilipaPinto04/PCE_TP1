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

@app.post("/Observation")
async def create_observation(data: dict):
    conn = None
    try:
        # Extração do ID numérico a partir da string "Patient/89980748"
        refer_string = data.get('refer', '')
        paciente_id = int(refer_string.split('/')[-1]) if '/' in refer_string else None

        conn = get_db_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            
            # 1. Tabela: observacoes
            cur.execute(
                """INSERT INTO observacoes (paciente_id, estado, refer, dataExecucao) 
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (paciente_id, data.get('estado'), refer_string, data.get('dataExecucao'))
            )
            obs_id = cur.fetchone()['id']

            # 2. Tabela: codigo
            obj_codigo = data.get('codigo', {})
            cur.execute(
                "INSERT INTO codigo (observacoes_id, text) VALUES (%s, %s) RETURNING id",
                (obs_id, obj_codigo.get('text'))
            )
            codigo_id = cur.fetchone()['id']

            # 3. Tabela: coding (Iteração sobre o array)
            for item in obj_codigo.get('coding', []):
                cur.execute(
                    """INSERT INTO coding (codigo_id, system, cod, disp) 
                       VALUES (%s, %s, %s, %s)""",
                    (codigo_id, item.get('system'), item.get('cod'), item.get('disp'))
                )

            # 4. Tabela: medicao
            m = data.get('medicao', {})
            cur.execute(
                """INSERT INTO medicao (observacoes_id, valor, unidade, sistema, cod) 
                   VALUES (%s, %s, %s, %s, %s)""",
                (obs_id, m.get('valor'), m.get('unidade'), m.get('sistema'), m.get('cod'))
            )

            conn.commit()

# --- PASSO B: MANDAR PARA O HAPI (FHIR) ---

        # 1. Preparar a lista de códigos (coding)
        lista_codigos = []
        for c in obj_codigo.get('coding', []):
            lista_codigos.append({
                "system": c.get('system'),
                "code": c.get('cod'),      # O HAPI exige a chave 'code'
                "display": c.get('disp')    # O HAPI exige a chave 'display'
            })

        # 2. Preparar os dados da medição
        dados_medicao = {
            "value": m.get('valor'),
            "unit": m.get('unidade'),
            "system": m.get('sistema'),
            "code": m.get('cod')
        }

        # 3. Montar o JSON final para o Servidor HAPI
        fhir_payload = {
            "resourceType": "Observation",
            "status": data.get('estado'),
            "subject": {"reference": refer_string},
            "effectiveDateTime": data.get('dataExecucao'),
            "code": {
                "coding": lista_codigos,
                "text": obj_codigo.get('text')
            },
            "valueQuantity": dados_medicao  # Aqui a chave tem de ser esta para o HAPI entender
        }

        hapi_url = os.getenv("HAPI_URL", "http://localhost:8080/fhir/Observation")
        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, timeout=5)
            hapi_status = hapi_res.status_code
        except Exception:
            hapi_status = "HAPI offline"

        return {
            "status": "sucesso",
            "db_id": obs_id,
            "hapi_status": hapi_status
        }

    except Exception as e:
        if conn: conn.rollback()
        raise HTTPException(status_code=500, detail=f"Erro no servidor: {str(e)}")
    finally:
        if conn: conn.close()

@app.get("/Patient/{patient_id}")
async def get_patient(patient_id: int):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 1. Buscar dados básicos do paciente
            cur.execute("SELECT * FROM patients WHERE id = %s", (patient_id,))
            patient = cur.fetchone()
            if not patient:
                raise HTTPException(status_code=404, detail="Paciente não encontrado")

            # 2. Buscar Telecoms do Paciente
            cur.execute("SELECT tipo, valor FROM telecom WHERE paciente_id = %s", (patient_id,))
            patient['telecom'] = cur.fetchall()

            # 3. Buscar Contactos e os seus detalhes (Telecom e Endereço)
            cur.execute("SELECT id, nome FROM contacto WHERE paciente_id = %s", (patient_id,))
            contactos = cur.fetchall()
            
            for con in contactos:
                # Telecoms do contacto
                cur.execute("SELECT tipo, valor FROM telecom WHERE contacto_id = %s", (con['id'],))
                con['telecom'] = cur.fetchall()
                # Endereços do contacto
                cur.execute("SELECT tipo, valor FROM endereco WHERE contacto_id = %s", (con['id'],))
                con['endereco'] = cur.fetchall()
            
            patient['contacto'] = contactos
            return patient

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn: conn.close()

    
@app.get("/Observation/{observation_id}")
async def get_observation(observation_id: int):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 1. Buscar a observação base
            cur.execute("SELECT * FROM observacoes WHERE id = %s", (observation_id,))
            obs = cur.fetchone()
            if not obs:
                raise HTTPException(status_code=404, detail="Observação não encontrada")

            # 2. Buscar o Código (text e coding)
            cur.execute("SELECT id, text FROM codigo WHERE observacoes_id = %s", (observation_id,))
            codigo_obj = cur.fetchone()
            if codigo_obj:
                cur.execute("SELECT system, cod, disp FROM coding WHERE codigo_id = %s", (codigo_obj['id'],))
                codigo_obj['coding'] = cur.fetchall()
                obs['codigo'] = codigo_obj

            # 3. Buscar a Medição
            cur.execute("SELECT valor, unidade, sistema, cod FROM medicao WHERE observacoes_id = %s", (observation_id,))
            obs['medicao'] = cur.fetchone()

            return obs

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn: conn.close()
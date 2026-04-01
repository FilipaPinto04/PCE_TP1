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
        # Extrair dados base
        nome_paciente = data.get('nome', 'Sem Nome')
        genero_raw = data.get('genero', 'unknown')

        conn = get_db_connection()
        # Usamos um cursor que persiste para todas as operações desta função
        cur = conn.cursor(cursor_factory=RealDictCursor)

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

        # Commit inicial para garantir que o paciente existe antes do HAPI ser chamado
        conn.commit()

        # --- PASSO B: MANDAR PARA O HAPI (FHIR) ---

        fhir_telecoms = [
            {"system": "phone" if t.get('tipo') == "telemóvel" else "email", "value": t.get('valor')}
            for t in data.get('telecom', [])
        ]

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

        fhir_payload = {
            "resourceType": "Patient",
            "name": [{"text": nome_paciente}],
            "gender": "male" if genero_raw == "m" else "female" if genero_raw == "f" else "unknown",
            "telecom": fhir_telecoms,
            "contact": fhir_contacts
        }

        hapi_url = "http://localhost:9000/fhir/Patient"
        headers = {
                "Content-Type": "application/fhir+json;charset=utf-8",
                "Accept": "application/fhir+json;charset=utf-8"
            }
        try:
            # Enviamos para a porta 9000
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers, timeout=5)
            
            if hapi_res.status_code in [200, 201]:
                # Extrair ID
                fhir_id_gerado = hapi_res.json().get('id')
                
                # UPDATE NO SQL (para deixar de estar [null])
                cur.execute(
                    "UPDATE patients SET fhir_id = %s WHERE id = %s",
                    (str(fhir_id_gerado), paciente_id)
                )
                conn.commit() # Grava a alteração
                
                return {
                    "mensagem": "Sincronizado com Sucesso!",
                    "id_local": paciente_id,
                    "id_fhir": fhir_id_gerado
                }
            else:
                # Se der erro, vamos ver o que o HAPI diz na consola
                print(f"Erro HAPI ({hapi_res.status_code}): {hapi_res.text}")
                return {
                    "mensagem": "Erro no HAPI",
                    "status": hapi_res.status_code,
                    "detalhe": hapi_res.text[:100]
                }
        except Exception as e:
            return {"mensagem": "HAPI Incontactável na porta 9000", "erro": str(e)}

    except Exception as e:
        if conn: conn.rollback()
        raise HTTPException(status_code=500, detail=f"Erro no processamento: {str(e)}")
    finally:
        if conn: 
            cur.close()
            conn.close()

@app.post("/Observation")
async def create_observation(data: dict):
    conn = None
    try:
        # 1. Extração do ID local (do SQL) enviado no JSON (ex: "Patient/1")
        refer_string = data.get('refer', '')
        local_patient_id = int(refer_string.split('/')[-1]) if '/' in refer_string else None

        if not local_patient_id:
            raise HTTPException(status_code=400, detail="Referência de paciente inválida. Use 'Patient/ID'")

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # --- PASSO A: TRADUÇÃO DE ID (SQL -> FHIR) ---
        # Vamos buscar o fhir_id que o HAPI conhece para este paciente
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (local_patient_id,))
        paciente_row = cur.fetchone()

        if not paciente_row or not paciente_row['fhir_id']:
            raise HTTPException(
                status_code=400, 
                detail=f"O paciente local {local_patient_id} não existe ou não foi sincronizado com o HAPI primeiro."
            )

        fhir_patient_id = paciente_row['fhir_id'] # Ex: "1000"

        # --- PASSO B: INSERIR NO TEU SQL (TABELAS LOCAIS) ---
        
        # 1. Tabela: observacoes
        cur.execute(
            """INSERT INTO observacoes (paciente_id, estado, refer, dataExecucao) 
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (local_patient_id, data.get('estado'), refer_string, data.get('dataExecucao'))
        )
        obs_id = cur.fetchone()['id']

        # 2. Tabela: codigo
        obj_codigo = data.get('codigo', {})
        cur.execute(
            "INSERT INTO codigo (observacoes_id, text) VALUES (%s, %s) RETURNING id",
            (obs_id, obj_codigo.get('text'))
        )
        codigo_id = cur.fetchone()['id']

        # 3. Tabela: coding
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

        # Grava os dados no teu Postgres
        conn.commit()

        # --- PASSO C: MANDAR PARA O HAPI (COM O ID TRADUZIDO) ---

        lista_codigos = [
            {
                "system": c.get('system'),
                "code": str(c.get('cod')),
                "display": c.get('disp')
            } for c in obj_codigo.get('coding', [])
        ]

        fhir_payload = {
            "resourceType": "Observation",
            "status": data.get('estado'),
            "subject": {"reference": f"Patient/{fhir_patient_id}"}, # <--- TRADUÇÃO AQUI!
            "effectiveDateTime": data.get('dataExecucao'),
            "code": {
                "coding": lista_codigos,
                "text": obj_codigo.get('text')
            },
            "valueQuantity": {
                "value": m.get('valor'),
                "unit": m.get('unidade'),
                "system": m.get('sistema'),
                "code": str(m.get('cod'))
            }
        }

        hapi_url = "http://localhost:9000/fhir/Observation"
        headers = {
            "Content-Type": "application/fhir+json;charset=utf-8",
            "Accept": "application/fhir+json;charset=utf-8"
        }

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers, timeout=10)
            
            if hapi_res.status_code in [200, 201]:
                fhir_obs_id = hapi_res.json().get('id')
                
                # --- PASSO D: GUARDAR O fhir_id DA OBSERVAÇÃO ---
                cur.execute(
                    "UPDATE observacoes SET fhir_id = %s WHERE id = %s",
                    (str(fhir_obs_id), obs_id)
                )
                conn.commit()
                
                return {
                    "mensagem": "Sucesso! Tradução feita e sincronizada.",
                    "id_local_obs": obs_id,
                    "id_fhir_obs": fhir_obs_id,
                    "paciente_mapeado": f"Local:{local_patient_id} -> FHIR:{fhir_patient_id}"
                }
            else:
                return {
                    "mensagem": "Gravado no SQL, mas o HAPI rejeitou o JSON",
                    "status_hapi": hapi_res.status_code,
                    "erro_detalhado": hapi_res.text[:300]
                }

        except Exception as hapi_err:
            return {"mensagem": "Gravado no SQL, HAPI offline", "erro": str(hapi_err)}

    except Exception as e:
        if conn: conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()

@app.get("/Patient/{local_id}")
async def get_patient(local_id: int):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 1. Perguntar ao SQL: "Qual é o fhir_id deste paciente local?"
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (local_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Paciente não existe no SQL")
        
        fhir_id = result.get('fhir_id')
        
        if not fhir_id:
            raise HTTPException(status_code=404, detail="Paciente existe no SQL, mas não foi sincronizado com o HAPI")

        # 2. Agora que temos o fhir_id (ex: '1000'), vamos ao HAPI (Porta 9000!)
        hapi_url = f"http://localhost:9000/fhir/Patient/{fhir_id}"
        
        headers = {"Accept": "application/fhir+json"}
        response = requests.get(hapi_url, headers=headers, timeout=5)

        if response.status_code == 200:
            return {
                "id_local": local_id,
                "id_fhir": fhir_id,
                "recurso_fhir_completo": response.json()
            }
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro ao buscar no HAPI")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()

    
@app.get("/Observation/{local_id}")
async def get_observation(local_id: int):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 1. Procurar o fhir_id na tua tabela 'observacoes'
        cur.execute("SELECT fhir_id FROM observacoes WHERE id = %s", (local_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Observação não encontrada no SQL")
        
        fhir_id = result.get('fhir_id')
        
        if not fhir_id:
            raise HTTPException(status_code=404, detail="Observação sem mapeamento FHIR (fhir_id é null)")

        # 2. Ir buscar ao HAPI usando o fhir_id (ex: '1001')
        # Porta 9000 e prefixo /fhir conforme o teu Docker
        hapi_url = f"http://localhost:9000/fhir/Observation/{fhir_id}"
        
        headers = {"Accept": "application/fhir+json"}
        response = requests.get(hapi_url, headers=headers, timeout=5)

        if response.status_code == 200:
            return {
                "id_local": local_id,
                "id_fhir": fhir_id,
                "dados_provenientes_do_hapi": response.json()
            }
        elif response.status_code == 404:
            raise HTTPException(status_code=404, detail="Observação não encontrada no servidor HAPI")
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro na comunicação com HAPI")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()

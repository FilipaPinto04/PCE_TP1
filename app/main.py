import psycopg2
import requests
from fastapi import FastAPI, HTTPException
from psycopg2.extras import RealDictCursor
import os
from datetime import datetime, timedelta
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi.security import OAuth2PasswordBearer
from fastapi import Depends

app = FastAPI()

# Configurações 
SECRET_KEY = "admin"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Para encriptar passwords e ler tokens
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

FHIR_SERVER_URL = os.getenv("FHIR_SERVER_URL", "http://localhost:9000/fhir")

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
                print("Tabelas inicializadas com sucesso via schema.sql!")
        else:
            print("Ficheiro schema.sql não encontrado. Ignorando init_db.")
            
    except Exception as e:
        print(f"Erro ao inicializar base de dados: {e}")
    finally:
        if conn:
            conn.close()

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Não foi possível validar as credenciais",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # Tenta ler o que está dentro do token usando a tua SECRET_KEY
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        return username
    except JWTError:
        raise credentials_exception
    
def validar_recurso_fhir(recurso_json, tipo_recurso):
    """
    Pergunta ao HAPI se o JSON é um recurso FHIR válido antes de o gravarmos.
    """
    url_valida = f"{FHIR_SERVER_URL}/{tipo_recurso}/$validate"
    try:
        res = requests.post(url_valida, json=recurso_json, timeout=5)
        resultado = res.json()
        
        # O HAPI devolve um OperationOutcome
        for issue in resultado.get('issue', []):
            if issue.get('severity') == 'error':
                return False, issue.get('diagnostics')
        return True, "Válido"
    except Exception as e:
        # Se o HAPI estiver offline, não conseguimos validar
        return False, f"Servidor de validação incontactável: {str(e)}"
    
@app.on_event("startup")
async def startup_event():
    # Aqui tu CHAMAS a função que definiste acima
    print("--- Verificando SQL Local ---")
    init_db() 
    
    print("--- Verificando Servidor FHIR ---")
    try:
        res = requests.get(f"{FHIR_SERVER_URL}/metadata", timeout=3)
        if res.status_code == 200:
            print("HAPI FHIR: Online e pronto.")
    except Exception:
        print("HAPI FHIR: Servidor offline.")

@app.post("/Register")
async def register(data: dict):
    username = data.get("username")
    password = data.get("password")
    
    # Encriptar a password
    hashed_pw = pwd_context.hash(password) 

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO usuarios (username, password_hash) VALUES (%s, %s)",
            (username, hashed_pw)
        )
        conn.commit()
        return {"msg": "Utilizador criado! Agora já podes fazer login."}
    except Exception as e:
        conn.rollback()
        return {"erro": str(e)}
    finally:
        conn.close()

@app.post("/Login")
async def login(data: dict):
    username = data.get("username")
    password = data.get("password")

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Procura o utilizador
    cur.execute("SELECT * FROM usuarios WHERE username = %s", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    # Se o utilizador não existir ou a password falhar
    if not user:
        raise HTTPException(status_code=401, detail="Utilizador não encontrado")
    
    # Verifica a password (compara o texto limpo com o hash do SQL)
    if not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Password incorreta")

    # Se chegou aqui, as credenciais estão certas! Gerar o Token
    access_token = create_access_token(data={"sub": user["username"]})
    
    # ESTE RETURN É O QUE O POSTMAN VAI MOSTRAR
    return {
        "access_token": access_token, 
        "token_type": "bearer"
    }

@app.post("/Patient")
async def create_patient(data: dict, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        # --- 1. PREPARAÇÃO DOS DADOS E PAYLOAD FHIR ---
        nome_paciente = data.get('nome', 'Sem Nome')
        genero_raw = data.get('genero', 'unknown')

        # Preparar Telecoms do Paciente para o FHIR
        fhir_telecoms = [
            {"system": "phone" if t.get('tipo') == "telemóvel" else "email", "value": t.get('valor')}
            for t in data.get('telecom', [])
        ]

        # Preparar Contactos para o FHIR
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
            "active": True,
            "name": [{"text": nome_paciente}],
            "gender": "male" if genero_raw == "m" else "female" if genero_raw == "f" else "unknown",
            "telecom": fhir_telecoms,
            "contact": fhir_contacts
        }

        # --- 2. VALIDAÇÃO FHIR (ANTES DE TOCAR NO SQL) ---
        valido, mensagem = validar_recurso_fhir(fhir_payload, "Patient")
        if not valido:
            raise HTTPException(status_code=400, detail=f"Erro de Schema FHIR: {mensagem}")

        # --- 3. SE VÁLIDO, INSERIR NO SQL LOCAL (TRANSAÇÃO COMPLETA) ---
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Inserir Paciente principal
        cur.execute(
            "INSERT INTO patients (nome, genero) VALUES (%s, %s) RETURNING id",
            (nome_paciente, genero_raw)
        )
        paciente_id_local = cur.fetchone()['id']

        # Inserir Telecoms do Paciente
        for tel in data.get('telecom', []):
            cur.execute(
                "INSERT INTO telecom (paciente_id, tipo, valor) VALUES (%s, %s, %s)",
                (paciente_id_local, tel.get('tipo'), tel.get('valor'))
            )

        # Inserir Contactos e seus detalhes (Maria, etc)
        for con in data.get('contacto', []):
            cur.execute(
                "INSERT INTO contacto (paciente_id, nome) VALUES (%s, %s) RETURNING id",
                (paciente_id_local, con.get('nome'))
            )
            contacto_id = cur.fetchone()['id']

            for tel_con in con.get('telecom', []):
                cur.execute(
                    "INSERT INTO telecom (contacto_id, tipo, valor) VALUES (%s, %s, %s)",
                    (contacto_id, tel_con.get('tipo'), tel_con.get('valor'))
                )
            
            for end in con.get('endereco', []):
                cur.execute(
                    "INSERT INTO endereco (contacto_id, tipo, valor) VALUES (%s, %s, %s)",
                    (contacto_id, end.get('tipo'), end.get('valor'))
                )

        conn.commit() # Grava tudo localmente com segurança

        # --- 4. ENVIO FINAL PARA O HAPI ---
        hapi_url = f"{FHIR_SERVER_URL}/Patient"
        headers = {"Content-Type": "application/fhir+json;charset=utf-8"}

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers, timeout=10)
            
            if hapi_res.status_code in [200, 201]:
                fhir_id_gerado = hapi_res.json().get('id')
                
                # UPDATE DO fhir_id NO SQL
                cur.execute(
                    "UPDATE patients SET fhir_id = %s WHERE id = %s",
                    (str(fhir_id_gerado), paciente_id_local)
                )
                conn.commit()
                
                return {
                    "status": "sucesso",
                    "id_local": paciente_id_local,
                    "id_fhir": fhir_id_gerado,
                    "mensagem": "Paciente e relacionamentos sincronizados!"
                }
            else:
                return {
                    "status": "aviso",
                    "id_local": paciente_id_local,
                    "msg": "Gravado localmente, mas rejeitado pelo HAPI final.",
                    "erro": hapi_res.text[:200]
                }
        except Exception as hapi_err:
            return {"status": "aviso", "id_local": paciente_id_local, "msg": "SQL OK, HAPI offline.", "erro": str(hapi_err)}

    except Exception as e:
        if conn: conn.rollback()
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=f"Erro no processamento: {str(e)}")
    finally:
        if conn: 
            cur.close()
            conn.close()

@app.post("/Observation")
async def create_observation(data: dict, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        # --- 1. PREPARAÇÃO E TRADUÇÃO DE IDs ---
        refer_string = data.get('refer', '')
        local_patient_id = int(refer_string.split('/')[-1]) if '/' in refer_string else None

        if not local_patient_id:
            raise HTTPException(status_code=400, detail="Referência de paciente inválida. Use 'Patient/ID'")

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Buscar o fhir_id do paciente no SQL
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (local_patient_id,))
        paciente_row = cur.fetchone()

        if not paciente_row or not paciente_row['fhir_id']:
            raise HTTPException(
                status_code=400, 
                detail=f"O paciente local {local_patient_id} não está sincronizado com o HAPI."
            )

        fhir_patient_id = paciente_row['fhir_id']

        # --- 2. MONTAR PAYLOAD E VALIDAR NO HAPI (ANTES DO SQL) ---
        obj_codigo = data.get('codigo', {})
        m = data.get('medicao', {})
        
        lista_codigos_fhir = [
            {
                "system": c.get('system'),
                "code": str(c.get('cod')),
                "display": c.get('disp')
            } for c in obj_codigo.get('coding', [])
        ]

        fhir_payload = {
            "resourceType": "Observation",
            "status": data.get('estado'),
            "subject": {"reference": f"Patient/{fhir_patient_id}"},
            "effectiveDateTime": data.get('dataExecucao'),
            "code": {
                "coding": lista_codigos_fhir,
                "text": obj_codigo.get('text')
            },
            "valueQuantity": {
                "value": m.get('valor'),
                "unit": m.get('unidade'),
                "system": m.get('sistema'),
                "code": str(m.get('cod'))
            }
        }

        # VALIDAÇÃO FHIR AQUI (Se falhar, o código para e não grava no SQL)
        valido, mensagem = validar_recurso_fhir(fhir_payload, "Observation")
        if not valido:
            raise HTTPException(status_code=400, detail=f"Erro de Schema FHIR: {mensagem}")

        # --- 3. INSERIR NO SQL LOCAL (SÓ SE FOR VÁLIDO) ---
        
        # Inserir Observação
        cur.execute(
            """INSERT INTO observacoes (paciente_id, estado, refer, dataExecucao) 
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (local_patient_id, data.get('estado'), refer_string, data.get('dataExecucao'))
        )
        obs_id = cur.fetchone()['id']

        # Inserir Código
        cur.execute(
            "INSERT INTO codigo (observacoes_id, text) VALUES (%s, %s) RETURNING id",
            (obs_id, obj_codigo.get('text'))
        )
        codigo_id = cur.fetchone()['id']

        # Inserir Codings
        for item in obj_codigo.get('coding', []):
            cur.execute(
                """INSERT INTO coding (codigo_id, system, cod, disp) 
                   VALUES (%s, %s, %s, %s)""",
                (codigo_id, item.get('system'), item.get('cod'), item.get('disp'))
            )

        # Inserir Medição
        cur.execute(
            """INSERT INTO medicao (observacoes_id, valor, unidade, sistema, cod) 
               VALUES (%s, %s, %s, %s, %s)""",
            (obs_id, m.get('valor'), m.get('unidade'), m.get('sistema'), m.get('cod'))
        )
        
        conn.commit() # Confirmar gravação local

        # --- 4. ENVIO FINAL PARA O HAPI ---
        hapi_url = f"{FHIR_SERVER_URL}/Observation"
        headers = {"Content-Type": "application/fhir+json;charset=utf-8"}

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers, timeout=10)
            
            if hapi_res.status_code in [200, 201]:
                fhir_obs_id = hapi_res.json().get('id')
                
                # Atualizar o fhir_id gerado
                cur.execute(
                    "UPDATE observacoes SET fhir_id = %s WHERE id = %s",
                    (str(fhir_obs_id), obs_id)
                )
                conn.commit()
                
                return {
                    "status": "sucesso",
                    "id_local": obs_id,
                    "id_fhir": fhir_obs_id,
                    "validação": "Passou no Schema FHIR"
                }
            else:
                return {"erro": "HAPI recusou", "detalhe": hapi_res.text}

        except Exception as hapi_err:
            return {"status": "Aviso", "msg": "Gravado no SQL, mas HAPI offline", "erro": str(hapi_err)}

    except Exception as e:
        if conn: conn.rollback()
        # Se for um erro que nós lançamos (HTTPException), passamos adiante
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()


@app.post("/Practitioner")
async def create_practitioner(data: dict, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        # --- 1. PREPARAÇÃO DOS DADOS ---
        nome_medico = data.get('nome', 'Médico Desconhecido')
        genero_raw = data.get('genero', 'unknown')
        especialidade = data.get('especialidade', 'Clínica Geral')

        # Tradução de género para o padrão FHIR
        fhir_gender = "male" if genero_raw == "m" else "female" if genero_raw == "f" else "unknown"

        # --- 2. MONTAR PAYLOAD E VALIDAR NO HAPI (ANTES DO SQL) ---
        fhir_payload = {
            "resourceType": "Practitioner",
            "name": [{"text": nome_medico}],
            "gender": fhir_gender,
            "qualification": [
                {
                    "code": {
                        "text": especialidade
                    }
                }
            ]
        }

        # VALIDAÇÃO FHIR (Se o HAPI não gostar do JSON, o código para aqui)
        valido, mensagem = validar_recurso_fhir(fhir_payload, "Practitioner")
        if not valido:
            raise HTTPException(status_code=400, detail=f"Erro no Schema Practitioner: {mensagem}")

        # --- 3. SE VÁLIDO, INSERIR NO SQL LOCAL ---
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute(
            "INSERT INTO medicos (nome, genero, especialidade) VALUES (%s, %s, %s) RETURNING id",
            (nome_medico, genero_raw, especialidade)
        )
        medico_id_local = cur.fetchone()['id']
        conn.commit()

        # --- 4. ENVIO DEFINITIVO PARA O HAPI ---
        hapi_url = f"{FHIR_SERVER_URL}/Practitioner"
        headers = {"Content-Type": "application/fhir+json;charset=utf-8"}

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers, timeout=10)

            if hapi_res.status_code in [200, 201]:
                fhir_id_gerado = hapi_res.json().get('id')

                # Atualizar o fhir_id no SQL para manter a sincronização
                cur.execute(
                    "UPDATE medicos SET fhir_id = %s WHERE id = %s",
                    (str(fhir_id_gerado), medico_id_local)
                )
                conn.commit()

                return {
                    "status": "sucesso",
                    "id_local": medico_id_local,
                    "id_fhir": fhir_id_gerado,
                    "mensagem": "Médico criado e sincronizado!"
                }
            else:
                return {
                    "status": "aviso",
                    "id_local": medico_id_local,
                    "mensagem": "Gravado localmente, mas falhou no HAPI final.",
                    "erro_hapi": hapi_res.text[:200]
                }
        except Exception as hapi_err:
            return {
                "status": "aviso",
                "id_local": medico_id_local,
                "mensagem": "Gravado localmente, mas HAPI offline.",
                "detalhe": str(hapi_err)
            }

    except Exception as e:
        if conn: conn.rollback()
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")
    finally:
        if conn:
            cur.close()
            conn.close()


@app.post("/Encounter")
async def create_encounter(data: dict, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        # 1. Extração dos IDs locais (ex: "Patient/1" -> 1)
        ref_paciente_local = data.get('refer_paciente', '')
        id_paciente_sql = int(ref_paciente_local.split('/')[-1]) if '/' in ref_paciente_local else None

        ref_medico_local = data.get('refer_medico', '')
        id_medico_sql = int(ref_medico_local.split('/')[-1]) if '/' in ref_medico_local else None

        if not id_paciente_sql:
            raise HTTPException(status_code=400, detail="Referência de paciente inválida.")

        # --- PASSO A: TRADUÇÃO DE IDs (SÓ CONSULTA, SEM INSERT AINDA) ---
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Buscar fhir_id do Paciente
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (id_paciente_sql,))
        row_p = cur.fetchone()
        if not row_p or not row_p['fhir_id']:
            raise HTTPException(status_code=400, detail="Paciente local não sincronizado com HAPI.")
        fhir_id_paciente = row_p['fhir_id']

        # Buscar fhir_id do Médico (se existir)
        fhir_id_medico = None
        if id_medico_sql:
            cur.execute("SELECT fhir_id FROM medicos WHERE id = %s", (id_medico_sql,))
            row_m = cur.fetchone()
            if row_m and row_m['fhir_id']:
                fhir_id_medico = row_m['fhir_id']

        # --- PASSO B: PREPARAR PAYLOAD E VALIDAR (ANTES DE GRAVAR NO SQL) ---
        lista_participantes = []
        if fhir_id_medico:
            lista_participantes.append({
                "individual": {"reference": f"Practitioner/{fhir_id_medico}"}
            })

        fhir_payload = {
            "resourceType": "Encounter",
            "status": "finished",
            "subject": {"reference": f"Patient/{fhir_id_paciente}"},
            "participant": lista_participantes,
            "period": {"start": data.get('data_consulta')},
            "type": [{"text": data.get('tipo_consulta')}]
        }

        # VALIDAÇÃO FHIR (Se falhar, o código para aqui e não suja o SQL)
        valido, mensagem = validar_recurso_fhir(fhir_payload, "Encounter")
        if not valido:
            raise HTTPException(status_code=400, detail=f"Erro no Schema Encounter: {mensagem}")

        # --- PASSO C: INSERIR NO SQL LOCAL (SÓ SE FOR VÁLIDO) ---
        try:
            # Inserir consulta
            cur.execute(
                """INSERT INTO consultas (paciente_id, medico_id, data_consulta, tipo_consulta) 
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (id_paciente_sql, id_medico_sql, data.get('data_consulta'), data.get('tipo_consulta'))
            )
            consulta_id_local = cur.fetchone()['id']

            # Inserir no histórico
            cur.execute(
                "INSERT INTO historico (paciente_id, consulta_id) VALUES (%s, %s)",
                (id_paciente_sql, consulta_id_local)
            )
            conn.commit()
        except Exception as sql_err:
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"Erro ao gravar no SQL local: {str(sql_err)}")

        # --- PASSO D: ENVIO FINAL PARA O HAPI ---
        hapi_url = f"{FHIR_SERVER_URL}/Encounter"
        headers = {"Content-Type": "application/fhir+json;charset=utf-8"}

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers, timeout=10)
            
            if hapi_res.status_code in [200, 201]:
                fhir_id_gerado = hapi_res.json().get('id')

                # Atualizar o fhir_id na tabela consultas
                cur.execute(
                    "UPDATE consultas SET fhir_id = %s WHERE id = %s",
                    (str(fhir_id_gerado), consulta_id_local)
                )
                conn.commit()

                return {
                    "status": "sucesso",
                    "id_local": consulta_id_local,
                    "id_fhir": fhir_id_gerado,
                    "info": "Consulta e Histórico criados e sincronizados."
                }
            else:
                return {
                    "status": "aviso",
                    "id_local": consulta_id_local,
                    "mensagem": "Gravado localmente, mas rejeitado pelo HAPI final.",
                    "erro": hapi_res.text[:200]
                }

        except Exception as hapi_err:
            return {"status": "aviso", "id_local": consulta_id_local, "msg": "SQL OK, HAPI offline.", "erro": str(hapi_err)}

    except Exception as e:
        if conn: conn.rollback()
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()


@app.get("/Patient/{local_id}")
async def get_patient(local_id: int, current_user: str = Depends(get_current_user)):
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
        hapi_url = f"{FHIR_SERVER_URL}/Patient/{fhir_id}"
        
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
async def get_observation(local_id: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        # Usando 'with' para garantir que o cursor feche automaticamente
        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            # 1. Procurar o fhir_id na tua tabela 'observacoes'
            cur.execute("SELECT fhir_id FROM observacoes WHERE id = %s", (local_id,))
            result = cur.fetchone()

            if not result:
                raise HTTPException(status_code=404, detail="Observação não encontrada no SQL")
            
            fhir_id = result.get('fhir_id')
            
            if not fhir_id:
                raise HTTPException(status_code=404, detail="Observação sem mapeamento FHIR (fhir_id é null)")

            # 2. Ir buscar ao HAPI usando o fhir_id
            hapi_url = f"{FHIR_SERVER_URL}/Observation/{fhir_id}"
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
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@app.get("/Observation")
async def get_patient_observations(patient: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 1. Traduzir o ID local do paciente para o fhir_id do HAPI
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (patient,))
        res_paciente = cur.fetchone()

        if not res_paciente or not res_paciente['fhir_id']:
            raise HTTPException(
                status_code=404, 
                detail="Paciente não encontrado ou não sincronizado com o HAPI"
            )

        fhir_patient_id = res_paciente['fhir_id']

        # 2. Consultar o HAPI usando o filtro de paciente (?patient=ID)
        # O HAPI permite filtrar recursos usando parâmetros de query
        hapi_url = f"{FHIR_SERVER_URL}/Observation?patient={fhir_patient_id}"
        
        headers = {"Accept": "application/fhir+json"}
        response = requests.get(hapi_url, headers=headers, timeout=5)

        if response.status_code == 200:
            # O HAPI devolve um "Bundle" (um pacote com uma lista de entradas)
            fhir_data = response.json()
            
            # Extraímos apenas a lista de observações para ser mais fácil de ler
            observacoes = []
            if "entry" in fhir_data:
                for entry in fhir_data["entry"]:
                    observacoes.append(entry["resource"])

            return {
                "id_local_paciente": patient,
                "id_fhir_paciente": fhir_patient_id,
                "total_observacoes": len(observacoes),
                "lista_observacoes": observacoes
            }
        else:
            raise HTTPException(
                status_code=response.status_code, 
                detail="Erro ao procurar observações no HAPI"
            )

    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()


@app.get("/Practitioner/{local_id}")
async def get_practitioner(local_id: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 1. Procurar o fhir_id na tua tabela local 'medicos'
        cur.execute("SELECT fhir_id FROM medicos WHERE id = %s", (local_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Médico não existe no SQL local")

        fhir_id = result.get('fhir_id')

        if not fhir_id:
            raise HTTPException(status_code=404, detail="Médico local sem fhir_id (não sincronizado com o HAPI)")

        # 2. Ir buscar ao HAPI os dados completos (Porta 9000 e prefixo /fhir)
        hapi_url = f"{FHIR_SERVER_URL}/Practitioner/{fhir_id}"
        
        headers = {"Accept": "application/fhir+json"}
        
        try:
            response = requests.get(hapi_url, headers=headers, timeout=5)
            
            if response.status_code == 200:
                return {
                    "id_local": local_id,
                    "id_fhir": fhir_id,
                    "recurso_fhir_do_servidor": response.json()
                }
            elif response.status_code == 404:
                raise HTTPException(status_code=404, detail="Médico não encontrado no servidor HAPI")
            else:
                raise HTTPException(status_code=response.status_code, detail="Erro ao comunicar com HAPI")
        
        except Exception as hapi_err:
            raise HTTPException(status_code=503, detail=f"Servidor HAPI incontactável: {str(hapi_err)}")

    except Exception as e:
        if isinstance(e, HTTPException): 
            raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()


@app.get("/Encounter/{consulta_id}")
async def get_encounter(consulta_id: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 1. Buscar o fhir_id na tua tabela local 'consultas'
        cur.execute("SELECT fhir_id FROM consultas WHERE id = %s", (consulta_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Consulta não encontrada no SQL local")
        
        fhir_id = result.get('fhir_id')
        
        if not fhir_id:
            raise HTTPException(
                status_code=404, 
                detail="Esta consulta existe no SQL, mas não tem um fhir_id (não foi sincronizada com o HAPI)"
            )

        # 2. Ir buscar o recurso ao HAPI usando o fhir_id (Porta 9000!)
        # O recurso FHIR para consultas chama-se "Encounter"
        hapi_url = f"{FHIR_SERVER_URL}/Encounter/{fhir_id}"
        
        headers = {"Accept": "application/fhir+json"}
        
        try:
            response = requests.get(hapi_url, headers=headers, timeout=5)
            
            if response.status_code == 200:
                return {
                    "id_local": consulta_id,
                    "id_fhir_no_hapi": fhir_id,
                    "recurso_fhir_completo": response.json()
                }
            elif response.status_code == 404:
                raise HTTPException(status_code=404, detail="Consulta não encontrada no servidor HAPI")
            else:
                raise HTTPException(status_code=response.status_code, detail="Erro na resposta do servidor HAPI")

        except Exception as hapi_err:
            raise HTTPException(status_code=503, detail=f"Servidor HAPI incontactável: {str(hapi_err)}")

    except Exception as e:
        # Se for um erro que nós já lançámos (HTTPException), passamos adiante
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")
    
    finally:
        if conn:
            cur.close()
            conn.close()

@app.get("/Patient/{local_id}/History")
async def get_patient_history(local_id: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 1. Descobrir qual é o ID do HAPI para este paciente
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (local_id,))
        result = cur.fetchone()
        
        if not result or not result['fhir_id']:
            raise HTTPException(status_code=404, detail="Paciente não encontrado ou sem ID FHIR")

        fhir_id = result['fhir_id']

        # 2. Pedir ao HAPI todas as Observations deste subject
        hapi_url = f"{FHIR_SERVER_URL}/Observation?subject=Patient/{fhir_id}"
        response = requests.get(hapi_url)

        if response.status_code == 200:
            return response.json() # Retorna o Bundle do FHIR com o histórico
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro no HAPI")

    finally:
        if conn: conn.close()
import psycopg2 # Se usares PostgreSQL
from psycopg2.extras import RealDictCursor
import os

# O 'host' deve ser o nome do serviço no docker-compose (neste caso, 'db')
DATABASE_URL = os.getenv("DATABASE_URL", "jdbc:postgresql://db:5432/hapi")

def get_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def get_patient_by_id(patient_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id, nome, apelido, data_nascimento FROM patients WHERE id = %s", (patient_id,))
        return cur.fetchone() # Devolve um dicionário com os dados do SQL

def get_observations_by_patient(patient_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id, tipo, valor, unidade, data FROM observations WHERE patient_id = %s", (patient_id,))
        return cur.fetchall() # Devolve uma lista de observações

def insert_patient(nome, apelido, data_nascimento):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO patients (nome, apelido, data_nascimento) VALUES (%s, %s, %s) RETURNING id",
            (nome, apelido, data_nascimento)
        )
        new_id = cur.fetchone()['id']
        conn.commit() # Importante para guardar os dados
        return new_id
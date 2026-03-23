-- 1. Tabela Principal de Pacientes
CREATE TABLE IF NOT EXISTS patients (
    id SERIAL PRIMARY KEY,
    nome VARCHAR(255) NOT NULL,
    genero CHAR(1)
);

-- 2. Telecom
CREATE TABLE IF NOT EXISTS telecom (
    id SERIAL PRIMARY KEY,
    paciente_id INT REFERENCES patients(id) ON DELETE CASCADE,
    tipo VARCHAR(50),
    valor VARCHAR(255)
);

-- 3. Contactos de Emergência
CREATE TABLE IF NOT EXISTS contacto (
    id SERIAL PRIMARY KEY,
    paciente_id INT REFERENCES patients(id) ON DELETE CASCADE,
    nome VARCHAR(255),
    telecom VARCHAR(50)
);

-- 4. Observações
CREATE TABLE IF NOT EXISTS observacoes (
    id SERIAL PRIMARY KEY,
    paciente_id INT REFERENCES patients(id),
    estado VARCHAR(50),
    codigo_loinc VARCHAR(20),
    codigo_display VARCHAR(100),
    data_execucao TIMESTAMP,
    valor_medicao FLOAT,
    unidade_medicao VARCHAR(10)
);
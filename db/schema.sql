CREATE TABLE IF NOT EXISTS patients (
    id SERIAL PRIMARY KEY,
    nome VARCHAR(255) NOT NULL,
    genero CHAR(1)
);

CREATE TABLE IF NOT EXISTS medicos (
    id SERIAL PRIMARY KEY,
    nome VARCHAR(255) NOT NULL,
    genero CHAR(1),
    especialidade VARCHAR(100)
);

CREATE TABLE IF NOT EXISTS contacto (
    id SERIAL PRIMARY KEY,
    paciente_id INT REFERENCES patients(id) ON DELETE CASCADE,
    medico_id INT REFERENCES medicos(id) ON DELETE CASCADE,
    nome VARCHAR(255)
);

CREATE TABLE IF NOT EXISTS telecom (
    id SERIAL PRIMARY KEY,
    paciente_id INT REFERENCES patients(id) ON DELETE CASCADE,
    contacto_id INT REFERENCES contacto(id) ON DELETE CASCADE,
    tipo VARCHAR(50),
    valor VARCHAR(255)
);

CREATE TABLE IF NOT EXISTS endereco (
    id SERIAL PRIMARY KEY,
    contacto_id INT REFERENCES contacto(id) ON DELETE CASCADE,
    tipo VARCHAR(50),
    valor VARCHAR (255)
);

CREATE TABLE IF NOT EXISTS consultas (
    id SERIAL PRIMARY KEY,
    paciente_id INT REFERENCES patients(id) ON DELETE CASCADE,
    medico_id INT REFERENCES medicos(id) ON DELETE SET NULL,
    data_consulta TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    tipo_consulta VARCHAR(100) 
);

CREATE TABLE IF NOT EXISTS observacoes (
    id SERIAL PRIMARY KEY,
    paciente_id INT REFERENCES patients(id) ON DELETE CASCADE,
    consulta_id INT REFERENCES consultas(id) ON DELETE CASCADE,
    estado VARCHAR(50),
    refer VARCHAR(70),
    dataExecucao TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS codigo (
    id SERIAL PRIMARY KEY,
    observacoes_id INT REFERENCES observacoes(id) ON DELETE CASCADE,
    text VARCHAR(150)
);

CREATE TABLE IF NOT EXISTS coding (
    id SERIAL PRIMARY KEY,
    codigo_id INT REFERENCES codigo(id) ON DELETE CASCADE,
    system VARCHAR(150),
    cod VARCHAR(150),
    disp VARCHAR(150)
);

CREATE TABLE IF NOT EXISTS medicao (
    id SERIAL PRIMARY KEY,
    observacoes_id INT REFERENCES observacoes(id) ON DELETE CASCADE,
    valor INTEGER,
    unidade VARCHAR(50),
    sistema VARCHAR(150),
    cod VARCHAR(50)
);

CREATE TABLE IF NOT EXISTS historico (
    id SERIAL PRIMARY KEY,
    paciente_id INT REFERENCES patients(id) ON DELETE CASCADE,
    consulta_id INT REFERENCES consultas(id) ON DELETE SET NULL
);
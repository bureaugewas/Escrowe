CREATE DATABASE IF NOT EXISTS clientdb;
USE clientdb;

CREATE TABLE customers (
  id INT PRIMARY KEY AUTO_INCREMENT COMMENT 'Surrogate key',
  name VARCHAR(120) NOT NULL,
  tier VARCHAR(20) NOT NULL COMMENT 'gold/silver/bronze',
  signup_date DATE NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT TRUE
) COMMENT = 'Customer master data';

CREATE TABLE orders (
  id INT PRIMARY KEY AUTO_INCREMENT,
  customer_id INT NOT NULL,
  region VARCHAR(20) NOT NULL,
  amount DECIMAL(10,2) NOT NULL COMMENT 'Order total in USD',
  placed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  is_paid BOOLEAN NOT NULL DEFAULT FALSE,
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers(id)
) COMMENT = 'Orders placed by customers';

INSERT INTO customers (name, tier, signup_date, is_active) VALUES
('Acme Corp', 'gold', '2021-03-01', TRUE),
('Globex', 'silver', '2021-06-15', TRUE),
('Initech', 'bronze', '2022-01-10', TRUE),
('Umbrella LLC', 'gold', '2020-11-20', FALSE),
('Soylent Inc', 'silver', '2022-08-02', TRUE),
('Hooli', 'gold', '2019-05-05', TRUE),
('Vehement Capital', 'bronze', '2023-02-14', TRUE),
('Massive Dynamic', 'silver', '2021-09-09', TRUE),
('Stark Industries', 'gold', '2018-12-01', TRUE),
('Wayne Enterprises', 'gold', '2017-07-04', TRUE);

INSERT INTO orders (customer_id, region, amount, placed_at, is_paid)
SELECT c.id, ELT(1 + (o MOD 4), 'US', 'EU', 'APAC', 'LATAM'),
      ROUND(10 + RAND(o) * 990, 2), DATE_ADD('2023-01-01', INTERVAL o DAY),
      (o MOD 3) != 0
FROM customers c
JOIN (SELECT @rownum := @rownum + 1 AS o FROM information_schema.columns, (SELECT @rownum := 0) r LIMIT 60) nums
ON 1=1;

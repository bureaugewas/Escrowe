IF DB_ID('clientdb') IS NULL
  CREATE DATABASE clientdb;
GO
USE clientdb;
GO

IF OBJECT_ID('dbo.orders') IS NOT NULL DROP TABLE dbo.orders;
IF OBJECT_ID('dbo.customers') IS NOT NULL DROP TABLE dbo.customers;
GO

CREATE TABLE customers (
  id INT IDENTITY(1,1) PRIMARY KEY,
  name VARCHAR(120) NOT NULL,
  tier VARCHAR(20) NOT NULL,
  signup_date DATE NOT NULL,
  is_active BIT NOT NULL DEFAULT 1
);
GO

CREATE TABLE orders (
  id INT IDENTITY(1,1) PRIMARY KEY,
  customer_id INT NOT NULL FOREIGN KEY REFERENCES customers(id),
  region VARCHAR(20) NOT NULL,
  amount DECIMAL(10,2) NOT NULL,
  placed_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
  is_paid BIT NOT NULL DEFAULT 0
);
GO

EXEC sys.sp_addextendedproperty @name=N'MS_Description',
  @value=N'Customer master data', @level0type=N'SCHEMA', @level0name=N'dbo',
  @level1type=N'TABLE', @level1name=N'customers';
GO

INSERT INTO customers (name, tier, signup_date, is_active) VALUES
('Acme Corp', 'gold', '2021-03-01', 1),
('Globex', 'silver', '2021-06-15', 1),
('Initech', 'bronze', '2022-01-10', 1),
('Umbrella LLC', 'gold', '2020-11-20', 0),
('Soylent Inc', 'silver', '2022-08-02', 1),
('Hooli', 'gold', '2019-05-05', 1),
('Vehement Capital', 'bronze', '2023-02-14', 1),
('Massive Dynamic', 'silver', '2021-09-09', 1),
('Stark Industries', 'gold', '2018-12-01', 1),
('Wayne Enterprises', 'gold', '2017-07-04', 1);
GO

INSERT INTO orders (customer_id, region, amount, placed_at, is_paid)
SELECT c.id,
      CASE (n.n % 4) WHEN 0 THEN 'US' WHEN 1 THEN 'EU' WHEN 2 THEN 'APAC' ELSE 'LATAM' END,
      CAST(10 + (RAND(CHECKSUM(NEWID())) * 990) AS DECIMAL(10,2)),
      DATEADD(day, n.n, '2023-01-01'),
      CASE WHEN n.n % 3 = 0 THEN 0 ELSE 1 END
FROM customers c
CROSS JOIN (VALUES (1),(2),(3),(4),(5),(6)) n(n);
GO

CREATE DATABASE IF NOT EXISTS db_catalog;
USE db_catalog;


CREATE TABLE categories (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(45) NOT NULL
);


CREATE TABLE tags (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(45) NOT NULL
);


CREATE TABLE products (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    public_id BINARY(16) NOT NULL,
    name VARCHAR(100) NOT NULL,
    categories_id INT NOT NULL,
    deleted_at DATETIME NULL,
    created_at DATETIME NOT NULL,
    sellers_id BIGINT NOT NULL, -- Referencia lógica a db_commerce.sellers(id)
    price DECIMAL(13,2) NOT NULL,
    CONSTRAINT fk_products_categories FOREIGN KEY (categories_id) REFERENCES categories (id)
);


CREATE TABLE tags_has_products (
    tags_id INT NOT NULL,
    products_id BIGINT NOT NULL,
    PRIMARY KEY (tags_id, products_id),
    CONSTRAINT fk_thp_tags FOREIGN KEY (tags_id) REFERENCES tags (id),
    CONSTRAINT fk_thp_products FOREIGN KEY (products_id) REFERENCES products (id)
);


CREATE TABLE product_revisions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    revision_date DATETIME NOT NULL,
    products_id BIGINT NOT NULL,
    price DECIMAL(13,2) NOT NULL,
    CONSTRAINT fk_product_revisions_products FOREIGN KEY (products_id) REFERENCES products (id)
);


CREATE TABLE discounts (
    id INT AUTO_INCREMENT PRIMARY KEY,
    title VARCHAR(45) NOT NULL,
    discount_type ENUM('PERCENTAGE', 'FIXED_AMOUNT') NOT NULL,
    discount_value DECIMAL(10,2) NOT NULL,
    start_date DATETIME NOT NULL,
    end_date DATETIME NOT NULL,
    is_active TINYINT NOT NULL DEFAULT 1,
    products_id BIGINT NOT NULL,
    CONSTRAINT fk_discounts_products FOREIGN KEY (products_id) REFERENCES products (id)
);


CREATE TABLE warehouses (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    address_id BIGINT NOT NULL, -- Referencia lógica a db_commerce.address(id)
    name VARCHAR(100) NOT NULL
);


CREATE TABLE inventory (
    id VARCHAR(45) PRIMARY KEY,
    warehouses_id BIGINT NOT NULL,
    products_id BIGINT NOT NULL,
    stock_quantity INT NOT NULL,
    CONSTRAINT fk_inventory_warehouses FOREIGN KEY (warehouses_id) REFERENCES warehouses (id),
    CONSTRAINT fk_inventory_products FOREIGN KEY (products_id) REFERENCES products (id)
);
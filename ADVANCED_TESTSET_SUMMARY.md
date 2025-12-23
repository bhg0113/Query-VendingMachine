# 🚀 고급 SQL 테스트셋 요약 (Advanced TestSet Summary)

## 📋 전체 39개 문제 목록

### 기본 + 보강 문제 (31개)
기존 31개 기본 문제 + 다음 3개 추가 보강 문제:

#### #32. 각 카테고리별로 출연한 배우 수
```sql
SELECT c.name, COUNT(DISTINCT fa.actor_id) AS actor_count 
FROM category c 
JOIN film_category fc ON c.category_id = fc.category_id 
JOIN film f ON fc.film_id = f.film_id 
LEFT JOIN film_actor fa ON f.film_id = fa.film_id 
GROUP BY c.name 
ORDER BY actor_count DESC;
```
**개념**: DISTINCT COUNT, 다중 JOIN, LEFT JOIN

#### #33. 각 고객별로 대여한 영화의 총 개수와 총 결제액
```sql
SELECT c.customer_id, c.first_name || ' ' || c.last_name AS customer_name, 
       COUNT(DISTINCT r.rental_id) AS rental_count, 
       ROUND(SUM(p.amount), 2) AS total_paid 
FROM customer c 
LEFT JOIN rental r ON c.customer_id = r.customer_id 
LEFT JOIN payment p ON c.customer_id = p.customer_id 
GROUP BY c.customer_id, c.first_name, c.last_name 
ORDER BY total_paid DESC LIMIT 5;
```
**개념**: LEFT JOIN 체인, 복합 집계, ROUND 함수

#### #34. 같은 배우가 출연한 영화들 중 가장 대여가 많이 된 영화
```sql
SELECT f.title, COUNT(r.rental_id) AS rental_count 
FROM film f 
JOIN film_actor fa ON f.film_id = fa.film_id 
JOIN actor a ON fa.actor_id = a.actor_id 
JOIN inventory i ON f.film_id = i.film_id 
JOIN rental r ON i.inventory_id = r.inventory_id 
GROUP BY a.actor_id, f.film_id, f.title 
HAVING COUNT(fa.actor_id) > 0 
ORDER BY rental_count DESC LIMIT 1;
```
**개념**: 5개 테이블 JOIN, HAVING 절 사용

---

### 고급 전용 문제 (8개)

#### #35. 주문 금액이 전체 평균 금액 이상인 주문의 개수
```sql
SELECT COUNT(*) 
FROM (SELECT p.payment_id, p.amount, AVG(p.amount) OVER () AS overall_avg 
      FROM payment p 
      WHERE p.amount >= (SELECT AVG(amount) FROM payment)) t;
```
**개념**: 윈도우 함수 OVER(), 서브쿼리, 집계

#### #36. 각 점포별로 인벤토리에 가장 많이 있는 영화
```sql
SELECT s.store_id, f.title, COUNT(i.inventory_id) AS inventory_count 
FROM store s 
JOIN inventory i ON s.store_id = i.store_id 
JOIN film f ON i.film_id = f.film_id 
GROUP BY s.store_id, f.film_id, f.title 
HAVING COUNT(i.inventory_id) = (SELECT MAX(count) 
                                 FROM (SELECT COUNT(i2.inventory_id) AS count 
                                       FROM inventory i2 
                                       WHERE i2.store_id = s.store_id 
                                       GROUP BY i2.film_id) t) 
ORDER BY s.store_id;
```
**개념**: 상관 서브쿼리, 최댓값 조건부 필터링

#### #37. 각 카테고리별 대여된 영화의 총 대여료 수익 상위 3개
```sql
SELECT c.name, 
       ROUND(SUM(f.rental_rate * COALESCE(
         (SELECT COUNT(*) FROM rental r 
          JOIN inventory i ON r.inventory_id = i.inventory_id 
          WHERE i.film_id = f.film_id), 0)), 2) AS total_revenue 
FROM category c 
JOIN film_category fc ON c.category_id = fc.category_id 
JOIN film f ON f.film_id = fc.film_id 
GROUP BY c.name 
ORDER BY total_revenue DESC LIMIT 3;
```
**개념**: 계산 필드, COALESCE, 중첩 서브쿼리

#### #38. 각 배우별 출연 영화의 평균 길이 대비 출연 배우 수 비교
```sql
SELECT a.first_name || ' ' || a.last_name AS actor_name, 
       COUNT(fa.film_id) AS film_count 
FROM actor a 
JOIN film_actor fa ON a.actor_id = fa.actor_id 
GROUP BY a.actor_id, a.first_name, a.last_name 
HAVING COUNT(fa.film_id) > (SELECT AVG(film_count) 
                            FROM (SELECT COUNT(fa.film_id) AS film_count 
                                  FROM actor a 
                                  JOIN film_actor fa ON a.actor_id = fa.actor_id 
                                  GROUP BY a.actor_id) AS avg_table) 
ORDER BY film_count DESC;
```
**개념**: HAVING과 서브쿼리 조합, 평균 초과 필터링

#### #39. 가장 높은 결제금액을 기록한 고객 3명
```sql
SELECT c.first_name || ' ' || c.last_name AS customer_name, 
       SUM(p.amount) AS total_paid 
FROM customer c 
JOIN payment p ON c.customer_id = p.customer_id 
GROUP BY c.customer_id, c.first_name, c.last_name 
ORDER BY total_paid DESC LIMIT 3;
```
**개념**: 상위 N개 조회, 정렬 + LIMIT

#### #40. 각 도시별로 고객 수와 평균 결제액 (5명 이상만)
```sql
SELECT ci.city, COUNT(DISTINCT c.customer_id) AS customer_count, 
       ROUND(AVG(p.amount), 2) AS avg_payment 
FROM city ci 
JOIN address a ON ci.city_id = a.city_id 
JOIN customer c ON a.address_id = c.address_id 
JOIN payment p ON c.customer_id = p.customer_id 
GROUP BY ci.city 
HAVING COUNT(DISTINCT c.customer_id) >= 5 
ORDER BY customer_count DESC;
```
**개념**: 4개 테이블 JOIN, DISTINCT COUNT, HAVING 필터링

#### #41. 각 영화 등급별 평균 대여 기간과 평균 교체 비용 (20 이상만)
```sql
SELECT rating, AVG(rental_duration) AS avg_rental_duration, 
       AVG(replacement_cost) AS avg_replacement_cost 
FROM film 
GROUP BY rating 
HAVING AVG(replacement_cost) >= 20 
ORDER BY avg_replacement_cost DESC;
```
**개념**: 다중 집계, HAVING 숫자 조건

---

## 🎓 고급 개념별 문제 분류

### 윈도우 함수 (Window Functions) - 5개 문제
- ROW_NUMBER() 누적
- RANK() / DENSE_RANK()
- SUM() OVER (누적합)
- AVG() OVER (이동평균)
- PARTITION BY와 ORDER BY 조합

**해당 문제**: #42-46

#### #42. 각 고객별 누적 대여 횟수 (ROW_NUMBER)
```sql
SELECT customer_id, rental_id, rental_date, 
       ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY rental_date ASC) AS cumulative_rental_count 
FROM rental 
ORDER BY customer_id, rental_date LIMIT 1;
```

#### #43. 특정 영화 쌍이 같은 고객에게 대여된 경우 (자기 조인)
```sql
SELECT f1.title AS film1, f2.title AS film2, COUNT(*) AS bundle_count 
FROM rental r1 
JOIN rental r2 ON r1.customer_id = r2.customer_id 
                  AND r1.rental_id != r2.rental_id 
                  AND r1.rental_id < r2.rental_id 
JOIN inventory i1 ON r1.inventory_id = i1.inventory_id 
JOIN inventory i2 ON r2.inventory_id = i2.inventory_id 
JOIN film f1 ON i1.film_id = f1.film_id 
JOIN film f2 ON i2.film_id = f2.film_id 
WHERE f1.film_id < f2.film_id 
GROUP BY f1.title, f2.title 
ORDER BY bundle_count DESC LIMIT 1;
```

#### #44. 결제 수행 날짜별 누적 매출액 (SUM OVER)
```sql
SELECT payment_date::date AS payment_date, amount, 
       SUM(amount) OVER (ORDER BY payment_date::date) AS cumulative_revenue 
FROM payment 
ORDER BY payment_date ASC LIMIT 1;
```

#### #45. 고객별 평균 결제액보다 많이 결제한 거래 (윈도우 AVG)
```sql
SELECT c.customer_id, c.first_name || ' ' || c.last_name AS customer_name, 
       p.amount, 
       ROUND(AVG(p.amount) OVER (PARTITION BY c.customer_id), 2) AS customer_avg_payment 
FROM customer c 
JOIN payment p ON c.customer_id = p.customer_id 
WHERE p.amount > (SELECT AVG(amount) FROM payment WHERE customer_id = c.customer_id) 
ORDER BY c.customer_id, p.amount DESC LIMIT 1;
```

#### #46. 각 카테고리별 영화를 배우 수로 순위 (RANK OVER)
```sql
SELECT c.name AS category_name, f.title, 
       COUNT(DISTINCT fa.actor_id) AS actor_count, 
       RANK() OVER (PARTITION BY c.name ORDER BY COUNT(DISTINCT fa.actor_id) DESC) AS actor_count_rank 
FROM category c 
JOIN film_category fc ON c.category_id = fc.category_id 
JOIN film f ON fc.film_id = f.film_id 
LEFT JOIN film_actor fa ON f.film_id = fa.film_id 
GROUP BY c.name, f.film_id, f.title 
ORDER BY c.name, actor_count_rank LIMIT 1;
```

---

### 복잡한 서브쿼리 (Complex Subqueries) - 4개 문제
- 상관 서브쿼리 (Correlated Subquery)
- 중첩 서브쿼리 (Nested Subquery)
- IN/EXISTS 연산자

**해당 문제**: #47-50

#### #47. 각 직원별 처리한 대여 건수와 수입
```sql
SELECT s.first_name || ' ' || s.last_name AS staff_name, 
       COUNT(r.rental_id) AS rental_count, 
       ROUND(SUM(f.rental_rate), 2) AS total_revenue 
FROM staff s 
JOIN rental r ON s.staff_id = r.staff_id 
JOIN inventory i ON r.inventory_id = i.inventory_id 
JOIN film f ON i.film_id = f.film_id 
GROUP BY s.staff_id, s.first_name, s.last_name 
ORDER BY rental_count DESC;
```

#### #48. 가장 많이 함께 출연한 배우 쌍 (자기 조인)
```sql
SELECT a1.first_name || ' ' || a1.last_name AS actor1, 
       a2.first_name || ' ' || a2.last_name AS actor2, 
       COUNT(*) AS shared_films 
FROM film_actor fa1 
JOIN film_actor fa2 ON fa1.film_id = fa2.film_id 
                    AND fa1.actor_id < fa2.actor_id 
JOIN actor a1 ON fa1.actor_id = a1.actor_id 
JOIN actor a2 ON fa2.actor_id = a2.actor_id 
GROUP BY fa1.actor_id, fa2.actor_id, a1.first_name, a1.last_name, a2.first_name, a2.last_name 
ORDER BY shared_films DESC LIMIT 1;
```

#### #49. 회원가입 이후 평균 대여 기간 (상관 서브쿼리)
```sql
SELECT ROUND(AVG(date_diff)) AS avg_days_between_rentals 
FROM (SELECT EXTRACT(DAY FROM (MAX(rental_date) - MIN(rental_date))) AS date_diff 
      FROM customer c 
      JOIN rental r ON c.customer_id = r.customer_id 
      GROUP BY c.customer_id 
      HAVING COUNT(r.rental_id) >= 4) AS rental_stats;
```

#### #50. 각 배우별 총 상영시간 (5000분 이상만)
```sql
SELECT a.first_name || ' ' || a.last_name AS actor_name, 
       SUM(f.length) AS total_length, 
       ROUND(AVG(f.length), 2) AS avg_length 
FROM actor a 
JOIN film_actor fa ON a.actor_id = fa.actor_id 
JOIN film f ON fa.film_id = f.film_id 
GROUP BY a.actor_id, a.first_name, a.last_name 
HAVING SUM(f.length) >= 5000 
ORDER BY total_length DESC;
```

---

### 다중 테이블 JOIN (Multiple Table Joins) - 3개 문제
- 4개 이상 테이블 조인
- 복합 JOIN 조건

**해당 문제**: #51-53

#### #51. 각 나라별 점포 수와 총 고객 수
```sql
SELECT c.country, COUNT(DISTINCT s.store_id) AS store_count, 
       COUNT(DISTINCT cust.customer_id) AS customer_count 
FROM country c 
JOIN city ci ON c.country_id = ci.country_id 
JOIN address a ON ci.city_id = a.city_id 
JOIN store s ON a.address_id = s.address_id 
JOIN customer cust ON a.address_id = cust.address_id 
GROUP BY c.country 
ORDER BY store_count DESC;
```

#### #52. 가장 자주 빌려주는 직원이 일하는 점포의 모든 직원 총 대여 건수
```sql
SELECT SUM(rental_count) 
FROM (SELECT s.staff_id, s.store_id, COUNT(*) AS rental_count 
      FROM rental r 
      JOIN staff s ON r.staff_id = s.staff_id 
      GROUP BY s.staff_id, s.store_id) staff_rentals 
WHERE store_id = (SELECT store_id 
                  FROM (SELECT s.staff_id, s.store_id, COUNT(*) AS rental_count 
                        FROM rental r 
                        JOIN staff s ON r.staff_id = s.staff_id 
                        GROUP BY s.staff_id, s.store_id 
                        ORDER BY rental_count DESC LIMIT 1) top_staff);
```

#### #53. 각 카테고리별 영화를 배우 수 기준으로 등급 매기기
```sql
SELECT c.name AS category_name, f.title, 
       COUNT(DISTINCT fa.actor_id) AS actor_count, 
       RANK() OVER (PARTITION BY c.name ORDER BY COUNT(DISTINCT fa.actor_id) DESC) AS actor_count_rank 
FROM category c 
JOIN film_category fc ON c.category_id = fc.category_id 
JOIN film f ON fc.film_id = f.film_id 
LEFT JOIN film_actor fa ON f.film_id = fa.film_id 
GROUP BY c.name, f.film_id, f.title 
ORDER BY c.name, actor_count_rank LIMIT 1;
```

---

### 날짜/시간 함수 (Date/Time Functions) - 2개 문제
- EXTRACT, TO_CHAR
- 날짜 연산
- HOUR, DAY, MONTH 추출

**해당 문제**: #54-55

#### #54. 월별 대여 트렌드 분석
```sql
SELECT TO_CHAR(rental_date, 'YYYY-MM') AS rental_month, 
       COUNT(*) AS rental_count, 
       ROUND(AVG((SELECT amount FROM payment WHERE payment.rental_id = rental.rental_id)), 2) AS avg_payment 
FROM rental 
GROUP BY TO_CHAR(rental_date, 'YYYY-MM') 
ORDER BY rental_month;
```

---

## 🎯 추천 학습 순서

### 레벨 1: 기초 강화 (문제 #32-36)
- 다중 JOIN 익숙해지기
- HAVING 절 활용
- 서브쿼리 기초

### 레벨 2: 중급 진출 (문제 #37-41)
- 복잡한 계산 필드
- 여러 집계 함수 조합
- HAVING + WHERE 조건 복합

### 레벨 3: 고급 도전 (문제 #42-46)
- 윈도우 함수 기초 (ROW_NUMBER, RANK)
- 누적 계산
- 자기 조인

### 레벨 4: 마스터 (문제 #47-50+)
- 복잡한 서브쿼리
- 상관 서브쿼리
- 고급 윈도우 함수

---

## 📊 SQL 개념별 빈도

| 개념 | 빈도 | 예제 문제 |
|------|------|---------|
| JOIN (2-3개) | ⭐⭐⭐⭐⭐ | #1-20 |
| JOIN (4개+) | ⭐⭐⭐⭐ | #24, #40, #51 |
| GROUP BY | ⭐⭐⭐⭐⭐ | 거의 모든 문제 |
| HAVING | ⭐⭐⭐⭐ | #16, #34, #38, #50 |
| 서브쿼리 | ⭐⭐⭐⭐ | #29, #36, #37, #49 |
| 윈도우 함수 | ⭐⭐⭐ | #42-46 |
| 날짜 함수 | ⭐⭐⭐ | #27, #28, #49, #54 |
| 자기 조인 | ⭐⭐⭐ | #43, #48 |
| DISTINCT | ⭐⭐⭐ | #32, #33, #40, #46 |
| CAST/타입 변환 | ⭐⭐ | #44, #54 |

---

## ✅ 성공 기준

### 기본 테스트셋
- **80% 이상**: SQL 기초 완성 ✅
- **90% 이상**: SQL 중급 진입 가능 🚀

### 고급 테스트셋
- **50% 이상**: 고급 SQL 학습 시작 가능 📚
- **70% 이상**: 고급 SQL 숙련 🎓
- **85% 이상**: SQL 마스터 레벨 🏆

---

**업데이트**: 2025년 11월 10일  
**작성자**: Query VendingMachine 팀















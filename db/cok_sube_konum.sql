-- Branches (subeler): location of each branch.
-- In a company with branches, each branch has its own location, and a vehicle's routes start
-- from and return to its branch. A branch without a location falls back to the company
-- headquarters. Companies without branches are not affected (their subeler table stays empty).

ALTER TABLE subeler ADD COLUMN IF NOT EXISTS konum_lat double precision;
ALTER TABLE subeler ADD COLUMN IF NOT EXISTS konum_lng double precision;

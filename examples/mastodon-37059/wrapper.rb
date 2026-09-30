# Run with bundle exec ruby /case/wrapper.rb, before Rails boots.
# This case declares accounts (Rails boot) and settings (target migration) required.
require 'json'
require 'pg'

connection = PG.connect(ENV.fetch('DATABASE_URL'))
begin
  missing = %w[public.accounts public.settings].any? do |table|
    connection.exec_params('SELECT to_regclass($1)', [table]).getvalue(0, 0).nil?
  end
ensure
  connection.close
end
if missing
  puts 'DBREDUCE_VERDICT ' + JSON.generate(reproduced: false, outcome: 'candidate_invalid')
  exit 0
end

# Unknown boot/migration failures retain their status and lack of a verdict.
exec('bundle', 'exec', 'rails', 'runner', File.join(__dir__, 'oracle.rb'))

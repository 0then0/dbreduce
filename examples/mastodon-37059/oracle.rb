# Run with: bundle exec rails runner /case/oracle.rb
# DATABASE_URL must point to the disposable DBReduce candidate.
load Rails.root.join('db/migrate/20251023210145_migrate_landing_page_setting.rb')

verdict = { reproduced: false }
begin
  ActiveRecord::Base.transaction(requires_new: true) do
    MigrateLandingPageSetting.new.migrate(:up)
    raise ActiveRecord::Rollback
  end
rescue ActiveRecord::RecordNotUnique => error
  cause = error.cause
  raise unless cause.is_a?(PG::UniqueViolation) &&
               cause.result.error_field(PG::Result::PG_DIAG_CONSTRAINT_NAME) == 'index_settings_on_var' &&
               cause.message.include?('Key (var)=(landing_page) already exists.')

  verdict = { reproduced: true, signature: 'mastodon-37059-landing-page-unique-violation' }

end
puts "DBREDUCE_VERDICT #{JSON.generate(verdict)}"

# Idempotent bootstrap for the throwaway local Redmine.
# Run: docker compose exec redmine bundle exec rails runner /seed/seed.rb
#
# Creates: REST API on, project "eco-test", users "me" (Manager) and
# "coworker" (Developer), and one deliberately blocked workflow transition
# (Manager, Bug tracker: Feedback -> Rejected) for outbox error testing.
# Prints the API keys as KEY=value lines.

if Redmine::DefaultData::Loader.no_data?
  Redmine::DefaultData::Loader.load('en')
end

Setting.rest_api_enabled = '1'
Setting.default_language = 'en'

def ensure_user(login, first, last)
  u = User.find_by_login(login) || User.new
  u.login = login
  u.firstname = first
  u.lastname = last
  u.mail = "#{login}@eco.test"
  u.password = u.password_confirmation = 'password123'
  u.must_change_passwd = false
  u.status = User::STATUS_ACTIVE
  u.save!
  u
end

me = ensure_user('me', 'Me', 'Tester')
coworker = ensure_user('coworker', 'Co', 'Worker')

project = Project.find_by_identifier('eco-test') || Project.new
project.name = 'Eco Test'
project.identifier = 'eco-test'
project.is_public = false
project.trackers = Tracker.all
project.enabled_module_names = %w[issue_tracking time_tracking]
project.save!

manager = Role.find_by_name('Manager')
developer = Role.find_by_name('Developer')
{ me => manager, coworker => developer }.each do |user, role|
  next if Member.where(project_id: project.id, user_id: user.id).exists?
  Member.create!(project: project, principal: user, role_ids: [role.id])
end

# Blocked transition for the "workflow-blocked push" scenario.
bug = Tracker.find_by_name('Bug')
feedback = IssueStatus.find_by_name('Feedback')
rejected = IssueStatus.find_by_name('Rejected')
WorkflowTransition.where(tracker_id: bug.id, role_id: manager.id,
                         old_status_id: feedback.id, new_status_id: rejected.id).delete_all

puts "REDMINE_PROJECT_ID=#{project.id}"
puts "REDMINE_KEY_ME=#{me.api_key}"
puts "REDMINE_KEY_COWORKER=#{coworker.api_key}"
puts "REDMINE_ME_ID=#{me.id}"
puts "REDMINE_COWORKER_ID=#{coworker.id}"

-- Phase 1 seed: en-US / US market defaults. Idempotent.
USE tg_x_copilot;

INSERT INTO locales (code, language_name, market, enabled, config) VALUES
  ('en-US', 'English (US)', 'US', 1, JSON_OBJECT('timezone', 'America/New_York', 'direction', 'ltr'))
ON DUPLICATE KEY UPDATE language_name=VALUES(language_name), market=VALUES(market);

INSERT INTO x_rules (locale, market, rule_key, rule_value, description) VALUES
  ('en-US','US','max_chars',            '280',  'Weighted X length limit for a standard post'),
  ('en-US','US','max_hashtags',         '1',    'Hashtags hurt reach when overused'),
  ('en-US','US','max_emojis',           '2',    'Keep tone natural'),
  ('en-US','US','max_images',           '4',    'X allows up to 4 images per post'),
  ('en-US','US','similarity_threshold', '0.72', 'Reject drafts too close to an English source (mere repost)'),
  ('en-US','US','banned_phrases', JSON_ARRAY(
      'you won''t believe', 'shocking', 'doctors hate', 'this changes everything',
      'nobody is talking about', 'game changer', 'mind-blowing', 'must see', 'gone wrong',
      'click here', 'link in bio', '100% guaranteed'), 'Misleading clickbait patterns'),
  ('en-US','US','style', JSON_QUOTE('Plain, conversational American English. Short sentences. Concrete specifics over adjectives. No ALL CAPS shouting.'), 'Style guide passed to the rewrite prompt')
ON DUPLICATE KEY UPDATE rule_value=VALUES(rule_value), description=VALUES(description);

INSERT INTO hooks (locale, market, name, pattern, example, weight) VALUES
  ('en-US','US','specific_number', 'Lead with the most surprising verified number from the source and what it means.',
     '3 of the 5 largest US banks quietly changed one fee this month. Here''s what it means for you:', 50),
  ('en-US','US','why_it_matters', 'State the news in one line, then "Why it matters:" with the practical consequence.',
     'Apple is moving iPhone assembly for the US market to India. Why it matters: prices, not patriotism.', 45),
  ('en-US','US','contrast', 'Contrast the common assumption with what the source actually shows (only if the source supports it).',
     'Everyone assumed EV sales were slowing. The Q3 registration data says otherwise.', 40),
  ('en-US','US','practical_takeaway', 'Open with what the reader can do or check today.',
     'If you use a password manager, check this setting today.', 40),
  ('en-US','US','question', 'Ask the question the audience already has, then answer it with the source facts.',
     'Why are airfares jumping right before the holidays? Three reasons, none of them fuel.', 30),
  ('en-US','US','first_hand', 'Highlight a first-hand detail (quote, document, photo) from the source.',
     'The internal memo is two paragraphs long. The second one is the story.', 25)
ON DUPLICATE KEY UPDATE pattern=VALUES(pattern), example=VALUES(example), weight=VALUES(weight);

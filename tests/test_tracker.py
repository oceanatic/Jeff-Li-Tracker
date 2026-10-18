import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update.py"
spec = importlib.util.spec_from_file_location("emerald_update", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class EmeraldRaceTests(unittest.TestCase):
    def test_exact_cutoff_and_accounts(self):
        self.assertEqual(mod.START_UNIX, 1792307345)
        self.assertEqual([(p['label'], p['gameName'], p['tagLine'], p['platform']) for p in mod.PLAYERS],
                         [('Jam', 'Jam', '0304', 'na1'), ('Antoñio', 'Antoñio', '9401', 'na1')])
        self.assertEqual(list(mod.fresh_state()['players']), ['Jam', 'Antoñio'])

    def test_rank_distances_from_both_sides(self):
        plat1 = {'tier': 'PLATINUM', 'division': 'I', 'lp': 50}
        plat1up = {'tier': 'PLATINUM', 'division': 'I', 'lp': 75}
        em4 = {'tier': 'EMERALD', 'division': 'IV', 'lp': 0}
        diam2 = {'tier': 'DIAMOND', 'division': 'II', 'lp': 45}
        diam3 = {'tier': 'DIAMOND', 'division': 'III', 'lp': 20}
        self.assertEqual(mod.distance_to_emerald(plat1), 50)
        self.assertEqual(mod.distance_to_emerald(em4), 0)
        self.assertEqual(mod.distance_to_emerald(diam2), 246)
        self.assertEqual(mod.race_position(plat1, plat1, 'up'), 0)
        self.assertEqual(mod.race_position(plat1up, plat1, 'up'), 0.5)
        self.assertEqual(mod.race_position(em4, plat1, 'up'), 1)
        self.assertEqual(mod.race_position(diam2, diam2, 'down'), 0)
        self.assertTrue(0 < mod.race_position(diam3, diam2, 'down') < 1)
        self.assertEqual(mod.race_position(em4, diam2, 'down'), 1)
        self.assertIsNone(mod.race_position(None, diam2, 'up'))

    def make_match(self, start, me_role='MIDDLE'):
        return {'info': {'queueId': 420, 'gameStartTimestamp': start*1000, 'gameDuration': 1800,
                         'participants': [
                            {'puuid': 'the-puuid', 'teamId': 100, 'participantId': 1, 'teamPosition': me_role,
                             'win': True, 'championId': 266, 'championName': 'Aatrox', 'kills': 7,
                             'deaths': 2, 'assists': 5, 'totalMinionsKilled': 180, 'neutralMinionsKilled': 12},
                            {'puuid': 'enemy-puuid', 'teamId': 200, 'participantId': 6, 'teamPosition': me_role}]}}

    def test_match_cutoff_and_non_support_gold(self):
        self.assertIsNone(mod.parse_match(self.make_match(mod.START_UNIX - 1), 'the-puuid'))
        row = mod.parse_match(self.make_match(mod.START_UNIX), 'the-puuid')
        self.assertEqual(row['kda'], 6)
        self.assertEqual(row['csPerMin'], 6.4)
        self.assertEqual(row['opponentId'], 6)
        self.assertEqual(row['role'], 'MIDDLE')
        supp = mod.parse_match(self.make_match(mod.START_UNIX, 'UTILITY'), 'the-puuid')
        self.assertEqual(supp['role'], 'UTILITY')
        self.assertIsNone(supp['opponentId'])
        gold = {'info': {'frames': [{'timestamp': 900000, 'participantFrames': {
            '1': {'totalGold': 7500}, '6': {'totalGold': 6600}}}]}}
        self.assertEqual(mod.frame_gold_lead_15(gold, 1, 6), 900)
        self.assertIsNone(mod.frame_gold_lead_15({'info': {'frames': []}}, 1, 6))

    def test_aggregate_ignores_missing_scores_and_support_gold(self):
        one = mod.parse_match(self.make_match(mod.START_UNIX), 'the-puuid')
        one.update({'goldLead15': 1200, 'aiScore': 71.5, 'fate': 'Godlike'})
        two = mod.parse_match(self.make_match(mod.START_UNIX+1, 'UTILITY'), 'the-puuid')
        two.update({'win': False, 'goldLead15': -2000, 'aiScore': None, 'fate': None,
                    'championName': 'Leona', 'championId': '89'})
        three = mod.parse_match(self.make_match(mod.START_UNIX+2, 'TOP'), 'the-puuid')
        three.update({'win': False, 'goldLead15': -500, 'aiScore': 42, 'fate': 'Doomed'})
        stats = mod.calculate_stats({'NA1_1': one, 'NA1_2': two, 'NA1_3': three})
        self.assertEqual(stats['games'], 3)
        self.assertEqual(stats['wins'], 1)
        self.assertAlmostEqual(stats['winrate'], 1/3)
        self.assertEqual(stats['averageAiScoreWin'], 71.5)
        self.assertEqual(stats['averageAiScoreLoss'], 42)
        self.assertEqual(stats['aiLossSampleCount'], 1)
        self.assertEqual(stats['averageGoldLead15NonSupport'], 350)
        self.assertEqual(stats['goldLead15SampleCount'], 2)
        self.assertEqual(stats['roleCounts']['UTILITY'], 1)
        self.assertEqual(stats['fateCounts']['Unavailable'], 1)
        self.assertEqual(stats['fateCounts']['Doomed'], 1)
        self.assertEqual(stats['mostPlayedChampion']['games'], 2)

    def test_collect_deduplicates_matches_and_uses_start_time(self):
        player = mod.PLAYERS[0]
        state = mod.fresh_player(player)
        rank = {'tier': 'GOLD', 'division': 'I', 'lp': 99, 'wins': 30, 'losses': 25}
        ids_calls = []
        def fake_ids(_, __, offset):
            ids_calls.append(offset)
            return ['NA1_a', 'NA1_b'] if offset == 0 else []
        def fake_match(mid, _, timeline=False):
            if timeline:
                return {'info': {'frames': [{'timestamp': 900000, 'participantFrames': {
                    '1': {'totalGold': 7000}, '6': {'totalGold': 6500}}}]}}
            return self.make_match(mod.START_UNIX if mid == 'NA1_a' else mod.START_UNIX - 1)
        with patch.object(mod, 'get_puuid', return_value='the-puuid'), \
             patch.object(mod, 'get_rank', return_value=rank), \
             patch.object(mod, 'get_match_ids', side_effect=fake_ids), \
             patch.object(mod, 'get_match', side_effect=fake_match), \
             patch.object(mod, 'deeplol_data', return_value={'aiScore': 80.5, 'fate': 'Solid'}):
            mod.update_player(state, player)
            self.assertEqual(state['stats']['games'], 1)
            self.assertEqual(state['stats']['averageGoldLead15NonSupport'], 500)
            self.assertEqual(state['stats']['averageAiScoreWin'], 80.5)
            self.assertEqual(state['initialRank']['tier'], 'GOLD')
            self.assertEqual(state['coverage']['pendingGames'], 0)
            mod.update_player(state, player)
            self.assertEqual(state['stats']['games'], 1)
            self.assertEqual(len(state['matches']), 1)
            self.assertEqual(state['raceProgress'], 0)
        self.assertTrue(ids_calls)

    def test_fate_is_estimate_only_with_complete_teammate_scores(self):
        players = [
            {'puu_id':'me','side':'BLUE','ai_score':50},
            {'puu_id':'ally1','side':'BLUE','ai_score':80},
            {'puu_id':'ally2','side':'BLUE','ai_score':70},
            {'puu_id':'enemy','side':'RED','ai_score':0},
        ]
        self.assertIn(mod.fate_from_scores(players, players[0]), mod.FATES)
        self.assertIsNone(mod.fate_from_scores(players, {'side':'BLUE','puu_id':'me','ai_score':None}))

if __name__ == '__main__':
    unittest.main()

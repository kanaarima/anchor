
from sqlalchemy.orm import sessionmaker, scoped_session, Session
from sqlalchemy.exc import ResourceClosedError
from sqlalchemy     import create_engine, func

from typing import Optional, Generator, List, Dict
from threading import Timer, Thread
from datetime import datetime

from .objects import (
    DBRelationship,
    DBRankHistory,
    DBBeatmap,
    DBMessage,
    DBChannel,
    DBScore,
    DBStats,
    DBUser,
    DBLog,
    Base
)

import traceback
import logging
import bancho

class Postgres:
    def __init__(self, username: str, password: str, host: str, port: int) -> None:
        self.logger = logging.getLogger('postgres')
        self.engine = create_engine(
            f'postgresql://{username}:{password}@{host}:{port}/{username}',
            max_overflow=30,
            pool_size=15,
            echo=False
        )

        Base.metadata.create_all(bind=self.engine)

        self.session_factory = scoped_session(
            sessionmaker(self.engine, expire_on_commit=False, autoflush=True)
        )
    
    @property
    def session(self) -> Session:
        for session in self.create_session():
            return session

    def create_session(self) -> Generator:
        session = self.session_factory()
        try:
            yield session
        except Exception as e:
            traceback.print_exc()
            self.logger.critical(f'Transaction failed: "{e}". Performing rollback...')
            session.rollback()
        finally:
            Timer(
                interval=15,
                function=self.close_session,
                args=[session]
            ).start()

    def close_session(self, session: Session) -> None:
        try:
            session.close()
        except AttributeError:
            pass
        except ResourceClosedError:
            pass
        except Exception as exc:
            self.logger.error(f'Failed to close session: {exc}')

    def user_by_name(self, name: str) -> Optional[DBUser]:
        return self.session.query(DBUser) \
                .filter(DBUser.name == name) \
                .first()
    
    def user_by_id(self, id: int) -> Optional[DBUser]:
        return self.session.query(DBUser) \
                .filter(DBUser.id == id) \
                .first()
    
    def beatmap_by_file(self, filename: str) -> Optional[DBBeatmap]:
        return self.session.query(DBBeatmap) \
                .filter(DBBeatmap.filename == filename) \
                .first()
    
    def beatmap_by_checksum(self, md5: str) -> Optional[DBBeatmap]:
        return self.session.query(DBBeatmap) \
                .filter(DBBeatmap.md5 == md5) \
                .first()
    
    def personal_best(self, beatmap_id: int, user_id: int, mode: int) -> Optional[DBScore]:
        return self.session.query(DBScore) \
                .filter(DBScore.beatmap_id == beatmap_id) \
                .filter(DBScore.user_id == user_id) \
                .filter(DBScore.mode == mode) \
                .filter(DBScore.status == 3) \
                .first()
    
    def channels(self) -> List[DBChannel]:
        return self.session.query(DBChannel).all()
    
    def stats(self, user_id: int, mode: int) -> Optional[DBStats]:
        return self.session.query(DBStats) \
                .filter(DBStats.user_id == user_id) \
                .filter(DBStats.mode == mode) \
                .first()
    
    def relationships(self, user_id: int) -> List[DBStats]:
        return self.session.query(DBRelationship) \
                .filter(DBRelationship.user_id == user_id) \
                .all()
    
    def add_relationship(self, user_id: int, target_id: int, friend: bool = True) -> DBRelationship:
        instance = self.session
        instance.add(
            rel := DBRelationship(
                user_id,
                target_id,
                int(not friend)
            )
        )
        instance.commit()

        return rel
    
    def remove_relationship(self, user_id: int, target_id: int, status: int = 0):
        instance = self.session
        rel = instance.query(DBRelationship) \
                .filter(DBRelationship.user_id == user_id) \
                .filter(DBRelationship.target_id == target_id) \
                .filter(DBRelationship.status == status)

        if rel.first():
            rel.delete()
            instance.commit()
    
    def submit_log(self, message: str, level: str, log_type: str):
        instance = self.session
        instance.add(
            DBLog(
                message,
                level,
                log_type
            )
        )
        instance.commit()
    
    def submit_message(self, sender: str, target: str, message: str):
        instance = self.session
        instance.add(
            DBMessage(
                sender,
                target,
                message
            )
        )
        instance.commit()

    def update_latest_activity(self, user_id: int):
        Thread(
            target=self.__update_latest_activity,
            args=[user_id],
            daemon=True
        ).start()

    def __update_latest_activity(self, user_id: int):
        instance = self.session
        instance.query(DBUser) \
                .filter(DBUser.id == user_id) \
                .update({
                    'latest_activity': datetime.now()
                })
        instance.commit()

    def update_rank_history(self, stats: DBStats, country: str):
        country_rank = bancho.services.cache.get_country_rank(stats.user_id, stats.mode, country)
        global_rank = bancho.services.cache.get_global_rank(stats.user_id, stats.mode)
        score_rank = bancho.services.cache.get_score_rank(stats.user_id, stats.mode)

        if global_rank <= 0:
            return

        instance = self.session
        instance.add(
            DBRankHistory(
                stats.user_id,
                stats.mode,
                stats.rscore,
                stats.pp,
                global_rank,
                country_rank,
                score_rank
            )
        )
        instance.commit()

    def restore_stats(self, user_id: int):
        instance = self.session_factory()

        # Recreate stats
        all_stats = [DBStats(user_id, mode) for mode in range(4)]

        # Get best scores
        best_scores = instance.query(DBScore) \
                    .filter(DBScore.user_id == user_id) \
                    .filter(DBScore.status == 3) \
                    .all()

        for mode in range(4):
            score_count = instance.query(DBScore) \
                        .filter(DBScore.user_id == user_id) \
                        .filter(DBScore.mode == mode) \
                        .count()

            combo_score = instance.query(DBScore) \
                        .filter(DBScore.user_id == user_id) \
                        .filter(DBScore.mode == mode) \
                        .order_by(DBScore.max_combo.desc()) \
                        .first()
            
            if combo_score:
                max_combo = combo_score.max_combo
            else:
                max_combo = 0

            total_score = instance.query(
                        func.sum(DBScore.total_score)) \
                        .filter(DBScore.user_id == user_id) \
                        .filter(DBScore.mode == mode) \
                        .scalar()
            
            if total_score is None:
                total_score = 0

            top_scores = self.top_scores(
                user_id=user_id,
                mode=mode
            )

            stats = all_stats[mode]

            if score_count > 0:
                total_acc = 0
                divide_total = 0

                for index, s in enumerate(top_scores):
                    add = 0.95 ** index
                    total_acc    += s.acc * add
                    divide_total += add

                if divide_total != 0:
                    stats.acc = total_acc / divide_total
                else:
                    stats.acc = 0.0

                weighted_pp = sum(score.pp * 0.95**index for index, score in enumerate(top_scores))
                bonus_pp = 416.6667 * (1 - 0.9994**score_count)

                stats.pp = weighted_pp + bonus_pp

            stats.playcount = score_count
            stats.max_combo = max_combo
            stats.tscore = total_score

        for score in best_scores:
            stats = all_stats[score.mode]

            grade_count = eval(f'stats.{score.grade.lower()}_count')

            if not grade_count:
                grade_count = 0

            if not stats.rscore:
                stats.rscore = 0

            if not stats.total_hits:
                stats.total_hits = 0

            stats.rscore += score.total_score
            grade_count += 1

            if stats.mode == 2:
                # ctb
                total_hits = score.n50 + score.n100 + score.n300 + score.nMiss + score.nKatu
            
            elif stats.mode == 3:
                # mania
                total_hits = score.n300 + score.n100 + score.n50 + score.nGeki + score.nKatu + score.nMiss
            
            else:
                # standard + taiko
                total_hits = score.n50 + score.n100 + score.n300 + score.nMiss

            stats.total_hits += total_hits

        for stats in all_stats:
            instance.add(stats)
        
        instance.commit()
        instance.close()

    def restore_hidden_scores(self, user_id: int):
        # This will restore all score status attributes

        self.logger.info(f'Restoring scores for user: {user_id}...')

        instance = self.session_factory()
        instance.query(DBScore) \
                    .filter(DBScore.user_id == user_id) \
                    .filter(DBScore.failtime != None) \
                    .filter(DBScore.status == -1) \
                    .update({
                        'status': 1
                    })
        instance.commit()

        all_scores = instance.query(DBScore) \
                    .filter(DBScore.user_id == user_id) \
                    .filter(DBScore.failtime == None) \
                    .filter(DBScore.status == -1) \
                    .all()

        # Sort scores by beatmap
        beatmaps: Dict[int, List[DBScore]] = {score.beatmap_id: [] for score in all_scores}

        for score in all_scores:
            beatmaps[score.beatmap_id].append(score)

        for beatmap, scores in beatmaps.items():
            scores.sort(
                key=lambda score: score.pp,
                reverse=True
            )

            best_score = scores[0]

            instance.query(DBScore) \
                    .filter(DBScore.id == best_score.id) \
                    .update({
                        'status': 3
                    })
            instance.commit()

            # Set other scores with same mods to 'submitted'
            instance.query(DBScore) \
                    .filter(DBScore.beatmap_id == beatmap) \
                    .filter(DBScore.user_id == user_id) \
                    .filter(DBScore.mods == best_score.mods) \
                    .filter(DBScore.status == -1) \
                    .update({
                        'status': 2
                    })
            instance.commit()

            all_mods = [score.mods for score in scores if score.mods != best_score.mods]

            for mods in all_mods:
                # Update best score with mods
                best_score = instance.query(DBScore) \
                    .filter(DBScore.beatmap_id == beatmap) \
                    .filter(DBScore.user_id == user_id) \
                    .filter(DBScore.mods == mods) \
                    .filter(DBScore.status == -1) \
                    .order_by(DBScore.total_score) \
                    .first()

                if not best_score:
                    continue

                best_score.status = 4
                instance.commit()

                instance.query(DBScore) \
                    .filter(DBScore.beatmap_id == beatmap) \
                    .filter(DBScore.user_id == user_id) \
                    .filter(DBScore.mods == mods) \
                    .filter(DBScore.status == -1) \
                    .update({
                        'status': 2
                    })
                instance.commit()

        instance.close()

        self.logger.info('Scores have been restored!')

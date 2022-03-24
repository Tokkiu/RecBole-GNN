# @Time   : 2022/3/15
# @Author : Lanling Xu
# @Email  : xulanling_sherry@163.com

r"""
DiffNet
################################################
Reference:
    Le Wu et al. "A Neural Influence Diffusion Model for Social Recommendation." in SIGIR 2019.

Reference code:
    https://github.com/PeiJieSun/diffnet
"""

import numpy as np
import torch

from recbole.model.init import xavier_uniform_initialization
from recbole.model.loss import EmbLoss
from recbole.utils import InputType

from recbole_graph.model.abstract_recommender import SocialRecommender
from recbole_graph.model.layers import BipartiteGCNConv


class DiffNet(SocialRecommender):
    r"""DiffNet is a deep influence propagation model to stimulate how users are influenced by the recursive social diffusion process for social recommendation.
    We implement the model following the original author with a pointwise training mode.
    """
    input_type = InputType.POINTWISE

    def __init__(self, config, dataset):
        super(DiffNet, self).__init__(config, dataset)

        # load dataset info
        self.edge_index, self.edge_weight = dataset.get_bipartite_inter_mat(row='user')
        self.edge_index, self.edge_weight = self.edge_index.to(self.device), self.edge_weight.to(self.device)

        self.net_edge_index, self.net_edge_weight = dataset.get_norm_net_adj_mat(row_norm=True)
        self.net_edge_index, self.net_edge_weight = self.net_edge_index.to(self.device), self.net_edge_weight.to(self.device)

        # load parameters info
        self.LABEL = config['LABEL_FIELD']
        self.embedding_size = config['embedding_size']  # int type:the embedding size of DiffNet
        self.n_layers = config['n_layers']  # int type:the GCN layer num of DiffNet for social net
        self.pretrained_review = config['pretrained_review']  # bool type:whether to load pre-trained review vectors of users and items
        self.loss_type = config['loss_type']  # string type:the loss type of optimization

        # define layers and loss
        self.user_embedding = torch.nn.Embedding(num_embeddings=self.n_users, embedding_dim=self.embedding_size)
        self.item_embedding = torch.nn.Embedding(num_embeddings=self.n_items, embedding_dim=self.embedding_size)
        self.bipartite_gcn_conv = BipartiteGCNConv(dim=self.embedding_size)

        if self.loss_type == 'BCE':
            self.loss_fct = torch.nn.BCELoss()
        elif self.loss_type == 'L2':
            self.loss_fct = EmbLoss()
        else:
            raise NotImplementedError("Make sure 'loss_type' in ['BCE', 'L2']!")

        # storage variables for full sort evaluation acceleration
        self.restore_user_e = None
        self.restore_item_e = None

        # parameters initialization
        self.apply(xavier_uniform_initialization)
        self.other_parameter_name = ['restore_user_e', 'restore_item_e']

        if self.pretrained_review:
            # handle review information, map the origin review into the new space
            self.user_review_embedding = torch.nn.Embedding(self.n_users, self.embedding_size, padding_idx=0)
            self.user_review_embedding.weight.requires_grad = False
            self.user_review_embedding.weight.data.copy_(self.convertDistribution(dataset.user_feat['user_review_emb']))

            self.item_review_embedding = torch.nn.Embedding(self.n_items, self.embedding_size, padding_idx=0)
            self.item_review_embedding.weight.requires_grad = False
            self.item_review_embedding.weight.data.copy_(self.convertDistribution(dataset.item_feat['item_review_emb']))

            self.user_fusion_layer = torch.nn.Linear(self.embedding_size, self.embedding_size)
            self.item_fusion_layer = torch.nn.Linear(self.embedding_size, self.embedding_size)
            self.activation = torch.nn.Sigmoid()

    def convertDistribution(self, x):
        mean, std = torch.mean(x), torch.std(x)
        y = (x - mean) * 0.2 / std
        return y

    def forward(self):
        user_embedding = self.user_embedding.weight
        final_item_embedding = self.item_embedding.weight

        if self.pretrained_review:
            user_reduce_dim_vector_matrix = self.activation(self.user_fusion_layer(self.user_review_embedding.weight))
            item_reduce_dim_vector_matrix = self.activation(self.item_fusion_layer(self.item_review_embedding.weight))

            user_review_vector_matrix = self.convertDistribution(user_reduce_dim_vector_matrix)
            item_review_vector_matrix = self.convertDistribution(item_reduce_dim_vector_matrix)

            user_embedding = user_embedding + user_review_vector_matrix
            final_item_embedding = final_item_embedding + item_review_vector_matrix

        user_embedding_from_consumed_items = self.bipartite_gcn_conv(x=(final_item_embedding, user_embedding), edge_index=self.edge_index, edge_weight=self.edge_weight, size=(self.n_items, self.n_users))

        embeddings_list = []
        for layer_idx in range(self.n_layers):
            user_embedding = self.bipartite_gcn_conv((user_embedding, user_embedding), self.net_edge_index, self.net_edge_weight, size=(self.n_users, self.n_users))
            embeddings_list.append(user_embedding)
        final_user_embedding = torch.stack(embeddings_list, dim=1)
        final_user_embedding = torch.sum(final_user_embedding, dim=1) + user_embedding_from_consumed_items

        return final_user_embedding, final_item_embedding

    def calculate_loss(self, interaction):
        # clear the storage variable when training
        if self.restore_user_e is not None or self.restore_item_e is not None:
            self.restore_user_e, self.restore_item_e = None, None

        user = interaction[self.USER_ID]
        item = interaction[self.ITEM_ID]
        label = interaction[self.LABEL]

        user_all_embeddings, item_all_embeddings = self.forward()
        u_embeddings = user_all_embeddings[user]
        i_embeddings = item_all_embeddings[item]
        prediction = torch.sigmoid(torch.mul(u_embeddings, i_embeddings).sum(dim=1))

        if self.loss_type == 'BCE':
            return self.loss_fct(prediction, label)
        else:  # self.loss_type = 'L2'
            loss = self.loss_fct(label - prediction, require_pow=True)
            return loss

    def predict(self, interaction):
        user = interaction[self.USER_ID]
        item = interaction[self.ITEM_ID]

        user_all_embeddings, item_all_embeddings = self.forward()

        u_embeddings = user_all_embeddings[user]
        i_embeddings = item_all_embeddings[item]
        scores = torch.mul(u_embeddings, i_embeddings).sum(dim=1)
        return scores

    def full_sort_predict(self, interaction):
        user = interaction[self.USER_ID]
        if self.restore_user_e is None or self.restore_item_e is None:
            self.restore_user_e, self.restore_item_e = self.forward()
        # get user embedding from storage variable
        u_embeddings = self.restore_user_e[user]

        # dot with all item embedding to accelerate
        scores = torch.matmul(u_embeddings, self.restore_item_e.transpose(0, 1))

        return scores.view(-1)